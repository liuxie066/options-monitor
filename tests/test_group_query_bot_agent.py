"""One natural-language turn: admission, context, fixed tools and render authority."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from om_group_query_bot.agent import (
    TOOL_NAMES,
    TOOLS,
    BotService,
    GroupQueryAgent,
)
from om_group_query_bot.config import BotSettings, load_bot_settings
from om_group_query_bot.feishu import EventRejected, VerifiedGroupEvent, message_text
from om_group_query_bot.store import (
    CONVERSATION_DB_NAME,
    ConversationStore,
    DeliveryStore,
)
from src.application.bot.model_config import ModelSettings

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
CLOCK = 1000.0


def _model_settings():
    return ModelSettings.from_config({
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "context_window_tokens": 65536,
        "max_output_tokens": 1000,
        "timeout_seconds": 30,
        "credential_name": "group_query_bot.llm.api_key",
        "max_attempts": 1,
    })


def _settings(tmp_path, **overrides):
    values = dict(
        instance_id="gq",
        data_dir=tmp_path / "data",
        app_id="cli_group",
        app_secret_ref="group_query_bot.feishu.app_secret",
        allowed_chat_ids=("oc_group",),
        model=_model_settings(),
        query_api_base_url="http://127.0.0.1:8765",
        query_api_token_ref="group_query_bot.query_api.token",
    )
    values.update(overrides)
    return BotSettings(**values)


def _bot_config_file(tmp_path, chats):
    path = tmp_path / "bot.json"
    path.write_text(json.dumps({
        "instance_id": "gq",
        "data_dir": str(tmp_path / "data"),
        "feishu": {"app_id": "cli_group",
                   "app_secret_ref": "group_query_bot.feishu.app_secret",
                   "allowed_chat_ids": chats},
        "model": {"provider": "deepseek", "model": "deepseek-chat",
                  "base_url": "https://api.deepseek.com",
                  "api_key_ref": "group_query_bot.llm.api_key",
                  "context_window_tokens": 65536,
                  "max_output_tokens": 1000,
                  "timeout_seconds": 30},
        "query_api": {"base_url": "http://127.0.0.1:8765",
                      "token_ref": "group_query_bot.query_api.token"},
    }))
    return path


def test_bot_config_accepts_sole_wildcard_chat_scope(tmp_path):
    assert load_bot_settings(_bot_config_file(tmp_path, ["*"])).allowed_chat_ids == ("*",)


def test_bot_config_rejects_wildcard_mixed_with_concrete_chats(tmp_path):
    with pytest.raises(ValueError, match="wildcard"):
        load_bot_settings(_bot_config_file(tmp_path, ["*", "oc_group"]))


def _event(sender="ou_alice", chat="oc_group", message_id="om_1", thread=None):
    return VerifiedGroupEvent(
        app_id="cli_group", tenant_key="tenant", sender_open_id=sender,
        sender_tenant_key="tenant", chat_id=chat, thread_id=thread,
        message_id=message_id, event_id="ev_" + message_id)


def _estimate(value):
    if isinstance(value, dict):
        return 100
    return 1 + sum(len(str(item.get("content") or "")) for item in value) // 4


def _text_response(text, tokens=10):
    return {"message": {"content": text, "tool_calls": []},
            "usage": {"completion_tokens": tokens}}


def _tool_response(name, arguments, call_id="call_1"):
    return {"message": {
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments)}}],
    }, "usage": {"completion_tokens": 10}}


class _ScriptedModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            return _text_response("")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeAPI:
    def __init__(self, data=None, error=None):
        self.calls = []
        self._data = data or {}
        self._error = error

    def call(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._data.get(kwargs["method"], self._default(kwargs["method"]))

    @staticmethod
    def _default(method):
        if method == "query_quotes":
            return {"quote_status": "current",
                    "underlier": {"last_price": 185.0, "bid": 184.9,
                                  "ask": 185.1,
                                  "last_price_update_time": "2026-11-10 01:00:00",
                                  "observed_at_utc": "2026-11-10T06:00:00+00:00"},
                    "option": None, "effective_parameters": {}}
        return {"status": "ok", "outcome": "none", "old_quote": None,
                "candidates": [], "displayed": 0, "omitted": 0,
                "reason_codes": [], "scope": {}, "evaluated_at_utc": None}


def _agent(tmp_path, *, model, api=None, settings=None, **overrides):
    settings = settings or _settings(tmp_path, **overrides)
    conversations = ConversationStore(settings.data_dir)
    agent = GroupQueryAgent(
        settings=settings, conversations=conversations,
        api_client=api or _FakeAPI(), api_key="dedicated-model-key",
        request=model, clock=lambda: CLOCK, now=lambda: NOW,
        estimate=_estimate)
    return agent, conversations


QUOTE_ARGUMENTS = {"symbol": "NVDA"}


def _roll_arguments(*, limit=10, contracts=2):
    return {
        "position": {
            "symbol": "NVDA",
            "contract": {"option_type": "put", "expiration": "2026-12-18",
                         "strike": 180.0},
            "side": "short",
            "contracts": contracts,
        },
        "search_bounds": {"max_new_expiration": "2027-03-19"},
        "limit": limit,
    }


def test_pure_clarification_is_admitted_and_rate_limited(tmp_path):
    model = _ScriptedModel([_text_response("请告诉我标的。"),
                            _text_response("请告诉我标的。")])
    agent, conversations = _agent(tmp_path, model=model, user_per_minute=1)

    first = agent.handle_event(_event(message_id="om_1"), "哪个好？")
    second = agent.handle_event(_event(message_id="om_2"), "哪个好？")

    assert first.status == "ok"
    assert first.text.startswith("请告诉我标的。")
    assert "未执行实时查询" in first.text
    assert second.status == "rate_limited"
    assert second.reason_codes == ("rate_limited",)
    assert "请求过于频繁" in second.text
    assert len(model.calls) == 1
    # The rejected turn is audited as metadata only.
    assert conversations.counts(NOW)["audit"] == 2


def test_replayed_model_turns_count_against_the_call_budget(tmp_path):
    model = _ScriptedModel([
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_1"),
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_2"),
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_3"),
    ])
    api = _FakeAPI()
    agent, _ = _agent(tmp_path, model=model, api=api, model_calls=2)

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert len(model.calls) == 2
    assert len(api.calls) == 2
    assert reply.status == "limited"
    assert "model_call_limit" in reply.reason_codes
    assert "185.0000" in reply.text


def test_tool_requests_share_one_absolute_turn_deadline(tmp_path):
    model = _ScriptedModel([
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_1"),
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_2"),
        _text_response("已连续查询两次。"),
    ])
    api = _FakeAPI()
    agent, _ = _agent(tmp_path, model=model, api=api)

    reply = agent.handle_event(_event(), "NVDA 现价，连查两次")

    assert reply.status == "ok"
    assert len(api.calls) == 2
    assert api.calls[0]["deadline_utc"] == api.calls[1]["deadline_utc"]
    assert api.calls[0]["deadline_utc"] == (
        NOW + timedelta(seconds=agent.settings.turn_seconds))


def test_model_output_cap_is_sent_to_the_provider_on_every_call(tmp_path):
    model = _ScriptedModel([
        _tool_response("query_quotes", QUOTE_ARGUMENTS),
        _text_response("185 美元。"),
    ])
    agent, _ = _agent(tmp_path, model=model)

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert reply.status == "ok"
    expected = min(int(agent.settings.output_tokens),
                   int(agent.settings.model.max_output_tokens
                       or agent.settings.output_tokens))
    assert [call["settings"].max_output_tokens for call in model.calls] == [
        expected, expected]


def test_finish_reason_length_marks_the_turn_limited(tmp_path):
    model = _ScriptedModel([{
        "message": {"content": "部分答复", "tool_calls": []},
        "usage": {"completion_tokens": 999},
        "finish_reason": "length",
    }])
    agent, _ = _agent(tmp_path, model=model)

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert reply.status == "limited"
    assert "output_token_limit" in reply.reason_codes


def test_context_is_limited_to_ten_complete_turns_newest_last(tmp_path):
    model = _ScriptedModel([_text_response("收到。")])
    agent, conversations = _agent(tmp_path, model=model)
    scope = _event().scope_sha256("gq")
    for index in range(1, 13):
        turn_id = f"turn{index:02d}"
        conversations.append_message(
            scope_sha256=scope, turn_id=turn_id, seq=0, role="user",
            content=f"历史问题 {index:02d}", now=NOW, source_id=f"om_{index}",
            source_expiry=NOW + timedelta(hours=24))
        conversations.append_message(
            scope_sha256=scope, turn_id=turn_id, seq=1, role="assistant",
            content=f"历史回答 {index:02d}", now=NOW, source_id=f"om_{index}",
            source_expiry=NOW + timedelta(hours=24))

    agent.handle_event(_event(), "本轮问题")

    messages = model.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    history = [item for item in messages[1:-1] if item["role"] == "user"]
    assert [item["content"] for item in history] == [
        f"历史问题 {index:02d}" for index in range(3, 13)]
    assert messages[-1] == {"role": "user", "content": "本轮问题"}


def test_configured_context_token_budget_drops_oversized_history(tmp_path):
    model = _ScriptedModel([_text_response("请补充标的。")])
    agent, conversations = _agent(tmp_path, model=model, context_tokens=1000)
    scope = _event().scope_sha256("gq")
    conversations.append_message(
        scope_sha256=scope, turn_id="huge", seq=0, role="user",
        content="历" * 5000, now=NOW, source_id="om_huge",
        source_expiry=NOW + timedelta(hours=24))
    conversations.append_message(
        scope_sha256=scope, turn_id="huge", seq=1, role="assistant",
        content="回答", now=NOW, source_id="om_huge",
        source_expiry=NOW + timedelta(hours=24))

    agent.handle_event(_event(), "本轮问题")

    messages = model.calls[0]["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "本轮问题"}


def test_two_users_in_one_group_do_not_share_context(tmp_path):
    model = _ScriptedModel([_text_response("请补充标的。"),
                            _text_response("请补充标的。")])
    agent, conversations = _agent(tmp_path, model=model)
    alice = _event(sender="ou_alice", message_id="om_a")
    bob = _event(sender="ou_bob", message_id="om_b")
    conversations.append_message(
        scope_sha256=alice.scope_sha256("gq"), turn_id="turn-a", seq=0,
        role="user", content="Alice 的私密问题", now=NOW, source_id="om_a",
        source_expiry=NOW + timedelta(hours=24))
    conversations.append_message(
        scope_sha256=alice.scope_sha256("gq"), turn_id="turn-a", seq=1,
        role="assistant", content="Alice 的回答", now=NOW, source_id="om_a",
        source_expiry=NOW + timedelta(hours=24))

    agent.handle_event(bob, "Bob 的问题")

    contents = [str(item.get("content")) for item in model.calls[0]["messages"]]
    assert not any("Alice" in text for text in contents)
    assert contents[-1] == "Bob 的问题"


def test_scenario_input_identity_is_reused_and_rotated_on_change(tmp_path):
    first_arguments = _roll_arguments(limit=10)
    second_arguments = _roll_arguments(limit=5)
    model = _ScriptedModel([
        _tool_response("evaluate_roll_down", first_arguments, "call_1"),
        _text_response("已按条件搜索。"),
        _tool_response("evaluate_roll_down", first_arguments, "call_2"),
        _text_response("已按条件搜索。"),
        _tool_response("evaluate_roll_down", second_arguments, "call_3"),
        _text_response("已按新条件搜索。"),
    ])
    api = _FakeAPI()
    agent, conversations = _agent(tmp_path, model=model, api=api,
                                  user_per_minute=30, model_calls=4)

    agent.handle_event(_event(message_id="om_1"), "把 NVDA put 往下移")
    agent.handle_event(_event(message_id="om_2"), "同样条件再看一次")
    agent.handle_event(_event(message_id="om_3"), "这次只看五个候选")

    scenario_calls = [call for call in api.calls
                      if call["method"] == "evaluate_roll_down"]
    assert len(scenario_calls) == 3
    identifiers = [call["input_id"] for call in scenario_calls]
    assert all(isinstance(item, str) and item for item in identifiers)
    assert identifiers[0] == identifiers[1]
    assert identifiers[0] != identifiers[2]
    assert all(call["input_as_of_utc"] == NOW for call in scenario_calls)
    # Three confirmed inputs: the shared identity plus the changed one.
    assert conversations.counts(NOW)["scenario_inputs"] == 2


def test_only_the_five_fixed_tools_exist_and_unknown_calls_are_refused(tmp_path):
    model = _ScriptedModel([
        _tool_response("option_positions_read", {"account": "lx"}, "call_1"),
        _text_response("没有可用的账户工具。"),
    ])
    api = _FakeAPI()
    agent, _ = _agent(tmp_path, model=model, api=api)

    assert TOOL_NAMES == {"query_quotes", "query_candidates", "evaluate_close",
                          "evaluate_roll_down", "evaluate_roll_up"}
    assert {tool["name"] for tool in TOOLS} == TOOL_NAMES
    reply = agent.handle_event(_event(), "查一下账户")

    assert api.calls == []
    assert "未执行实时查询" in reply.text


def test_symbol_schema_accepts_user_spelling():
    from om_group_query_bot.agent import SYSTEM_PROMPT

    assert "不要要求用户提供市场前缀" in SYSTEM_PROMPT
    symbol_fields = []
    def walk(schema):
        for name, prop in (schema.get("properties") or {}).items():
            if name == "symbol":
                symbol_fields.append(prop)
            elif isinstance(prop, dict) and "properties" in prop:
                walk(prop)
    for tool in TOOLS:
        walk(tool["input_schema"])
    assert len(symbol_fields) == 5
    assert all("不要追问市场前缀" in field.get("description", "")
               for field in symbol_fields)


def test_host_request_identity_is_not_model_supplied(tmp_path):
    model = _ScriptedModel([
        _tool_response("query_quotes", {"symbol": "US.NVDA",
                                        "request_id": "same-forever"}, "call_1"),
        _text_response("查询完成。"),
    ])
    api = _FakeAPI()
    agent, _ = _agent(tmp_path, model=model, api=api)

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert api.calls == []
    assert "未执行实时查询" in reply.text


def test_invented_number_is_rejected_and_the_model_rewrites(tmp_path):
    model = _ScriptedModel([
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_1"),
        _text_response("刚才看到的价格是 999.99。"),
        _text_response("NVDA 最新价 185.0000，买一 184.9000 / 卖一 185.1000。"),
    ])
    agent, _ = _agent(tmp_path, model=model, api=_FakeAPI())

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert reply.status == "ok"
    # The first narration invented a number; the host asked for a rewrite and
    # sent the corrected model reply without appending a structured block.
    assert reply.text == "NVDA 最新价 185.0000，买一 184.9000 / 卖一 185.1000。"
    assert "999.99" not in reply.text
    assert reply.reason_codes == ("reply_guard_retry",)
    correction = model.calls[2]["messages"][-1]
    assert correction["role"] == "user"
    assert "999.99" in correction["content"]


def test_second_guard_failure_falls_back_to_host_wording(tmp_path):
    model = _ScriptedModel([
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_1"),
        _text_response("价格是 999.99。"),
        _text_response("价格是 999.98。"),
    ])
    agent, _ = _agent(tmp_path, model=model, api=_FakeAPI())

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert reply.status == "ok"
    assert "reply_guard_fallback" in reply.reason_codes
    assert "185.0000" in reply.text
    assert "999.9" not in reply.text


def test_valid_model_narration_is_sent_as_is(tmp_path):
    model = _ScriptedModel([
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_1"),
        _text_response("NVDA 最新价 185.0000，买一 184.9000 / 卖一 185.1000。"),
    ])
    agent, _ = _agent(tmp_path, model=model, api=_FakeAPI())

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert reply.text == "NVDA 最新价 185.0000，买一 184.9000 / 卖一 185.1000。"
    assert "查询结果：" not in reply.text
    assert reply.reason_codes == ()


def test_budget_exhaustion_returns_a_controlled_note_with_the_render(tmp_path):
    model = _ScriptedModel([
        _tool_response("query_quotes", QUOTE_ARGUMENTS, "call_1"),
    ])
    agent, _ = _agent(tmp_path, model=model, model_calls=1)

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert reply.status == "limited"
    assert "本次查询在限制内未能完成（model_call_limit）" in reply.text
    assert "185.0000" in reply.text


def test_model_transport_failure_never_claims_success(tmp_path):
    model = _ScriptedModel([RuntimeError("socket reset")])
    api = _FakeAPI()
    agent, _ = _agent(tmp_path, model=model, api=api)

    reply = agent.handle_event(_event(), "NVDA 现价")

    assert reply.status == "model_unavailable"
    assert reply.reason_codes == ("model_transport_failed",)
    assert reply.text == "自然语言服务当前不可用，请稍后重试。"
    assert api.calls == []


def test_admission_failure_never_calls_model_or_api(tmp_path):
    model = _ScriptedModel([_text_response("不应到达。")])
    agent, _ = _agent(tmp_path, model=model, bot_concurrency=1)
    permit = agent.admission.acquire(sender="ou_other", chat="oc_group")
    try:
        reply = agent.handle_event(_event(), "NVDA 现价")
    finally:
        permit.release()

    assert reply.status == "rate_limited"
    assert reply.reason_codes == ("bot_concurrency_limited",)
    assert model.calls == []


def test_empty_message_is_audited_without_model_calls(tmp_path):
    model = _ScriptedModel([_text_response("不应到达。")])
    agent, conversations = _agent(tmp_path, model=model)

    reply = agent.handle_event(_event(), "   ")

    assert reply.status == "input_error"
    assert reply.reason_codes == ("empty_message",)
    assert model.calls == []
    assert conversations.counts(NOW)["audit"] == 1


class _Secrets:
    def __init__(self):
        self.values = {
            "group_query_bot.feishu.app_secret": "feishu-app-secret",
            "group_query_bot.llm.api_key": "dedicated-model-key",
            "group_query_bot.query_api.token": "dedicated-query-token",
        }

    def get(self, logical_name, legacy_env_name=None):
        return self.values.get(logical_name)


class _Replies:
    def __init__(self):
        self.calls = []

    def __call__(self, *, message_id, text, uuid, reply_in_thread, before_send):
        self.calls.append({"message_id": message_id, "text": text, "uuid": uuid,
                           "reply_in_thread": reply_in_thread})
        before_send()
        return {"code": 0, "data": {"message_id": "om_reply_" + message_id}}


def _payload(message_id="om_1", sender="ou_alice", chat="oc_group"):
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1",
                   "app_id": "cli_group", "tenant_key": "tenant"},
        "event": {
            "sender": {"sender_id": {"open_id": sender}, "sender_type": "user",
                       "tenant_key": "tenant"},
            "message": {
                "chat_id": chat, "chat_type": "group",
                "message_id": message_id, "message_type": "text",
                "content": json.dumps({"text": "@_user_1 你好"}),
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}],
            },
        },
    }


def test_message_text_strips_mentions_and_rejects_invalid_content():
    assert message_text(_payload()) == "你好"

    mention_only = _payload()
    mention_only["event"]["message"]["content"] = json.dumps(
        {"text": "@_user_1   "})
    with pytest.raises(EventRejected) as empty:
        message_text(mention_only)
    assert str(empty.value) == "empty_message"

    too_long = _payload()
    too_long["event"]["message"]["content"] = json.dumps({"text": "长" * 4001})
    with pytest.raises(EventRejected) as long_message:
        message_text(too_long)
    assert str(long_message.value) == "message_too_long"

    non_text = _payload()
    non_text["event"]["message"]["message_type"] = "image"
    with pytest.raises(EventRejected) as unsupported:
        message_text(non_text)
    assert str(unsupported.value) == "unsupported_message"

    broken = _payload()
    broken["event"]["message"]["content"] = "{not-json"
    with pytest.raises(EventRejected) as malformed:
        message_text(broken)
    assert str(malformed.value) == "unsupported_message"


def _service(tmp_path, model, replies):
    return BotService(
        _settings(tmp_path, user_per_minute=30), secret_provider=_Secrets(),
        request=model, reply_fn=replies, now=lambda: NOW,
        clock=lambda: CLOCK)


def test_bot_service_physically_cleans_expired_bodies_at_startup(tmp_path):
    settings = _settings(tmp_path, user_per_minute=30)
    conversations = ConversationStore(settings.data_dir)
    conversations.append_message(
        scope_sha256="scope-a", turn_id="turn-old", seq=0, role="user",
        content="过期正文", now=NOW - timedelta(hours=25), source_id="om_old",
        source_expiry=NOW - timedelta(hours=1))
    conversations.append_message(
        scope_sha256="scope-a", turn_id="turn-old", seq=1, role="assistant",
        content="过期回复", now=NOW - timedelta(hours=25), source_id="om_old",
        source_expiry=NOW - timedelta(hours=1))
    deliveries = DeliveryStore(settings.data_dir, db_name=CONVERSATION_DB_NAME)
    deliveries.claim(_event(message_id="om_old_delivery"), "gq",
                     NOW - timedelta(days=8))

    BotService(settings, secret_provider=_Secrets(), request=_ScriptedModel([]),
               reply_fn=_Replies(), now=lambda: NOW, clock=lambda: CLOCK)

    import sqlite3
    db = sqlite3.connect(conversations.path)
    try:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    finally:
        db.close()


def test_bot_service_delivers_once_and_survives_restart(tmp_path):
    model = _ScriptedModel([_text_response("请告诉我标的。")])
    replies = _Replies()
    service = _service(tmp_path, model, replies)

    result = service.handle_payload(_payload(), "ou_bot", text="你好")

    assert result["status"] == "acknowledged"
    assert len(replies.calls) == 1
    assert replies.calls[0]["text"].startswith("请告诉我标的。")
    assert service.deliveries.counts(NOW) == {"acknowledged": 1}
    assert service.conversations.counts(NOW)["outbox_payloads"] == 1

    duplicate = service.handle_payload(_payload(), "ou_bot", text="你好")
    assert duplicate == {"status": "duplicate", "delivery_state": "acknowledged"}
    assert len(replies.calls) == 1

    restarted_model = _ScriptedModel([])
    restarted_replies = _Replies()
    restarted = _service(tmp_path, restarted_model, restarted_replies)
    after_restart = restarted.handle_payload(_payload(), "ou_bot", text="你好")
    assert after_restart == {"status": "duplicate",
                             "delivery_state": "acknowledged"}
    assert restarted_replies.calls == []
    assert restarted_model.calls == []


def test_reply_failure_before_send_is_terminal_failed(tmp_path):
    model = _ScriptedModel([_text_response("请告诉我标的。")])

    def failing_reply(**kwargs):
        raise RuntimeError("transport refused")

    service = BotService(_settings(tmp_path, user_per_minute=30),
                         secret_provider=_Secrets(), request=model,
                         reply_fn=failing_reply, now=lambda: NOW,
                         clock=lambda: CLOCK)
    result = service.handle_payload(_payload("om_fail"), "ou_bot", text="你好")

    assert result["status"] == "failed"
    assert service.deliveries.counts(NOW) == {"failed": 1}


def test_reply_failure_after_send_is_outcome_unknown(tmp_path):
    model = _ScriptedModel([_text_response("请告诉我标的。")])

    def unknown_reply(*, before_send, **kwargs):
        before_send()
        raise RuntimeError("connection dropped")

    service = BotService(_settings(tmp_path, user_per_minute=30),
                         secret_provider=_Secrets(), request=model,
                         reply_fn=unknown_reply, now=lambda: NOW,
                         clock=lambda: CLOCK)
    result = service.handle_payload(_payload("om_unknown"), "ou_bot",
                                    text="你好")

    assert result["status"] == "outcome_unknown"
    assert service.deliveries.counts(NOW) == {"outcome_unknown": 1}


def test_clarification_note_is_not_accumulated_across_turns(tmp_path):
    from om_group_query_bot.render import CLARIFICATION_NOTE

    model = _ScriptedModel([
        # Worst case: the model echoes a note it saw in older history.
        _text_response(f"请补充张数。\n\n{CLARIFICATION_NOTE}"),
    ])
    agent, conversations = _agent(tmp_path, model=model)
    scope = _event().scope_sha256("gq")
    conversations.append_message(
        scope_sha256=scope, turn_id="legacy", seq=0, role="user",
        content="帮我看看平仓", now=NOW, source_id="om_legacy",
        source_expiry=NOW + timedelta(hours=24))
    conversations.append_message(
        scope_sha256=scope, turn_id="legacy", seq=1, role="assistant",
        content=f"请补充DTE。\n\n{CLARIFICATION_NOTE}", now=NOW,
        source_id="om_legacy", source_expiry=NOW + timedelta(hours=24))

    reply = agent.handle_event(_event(), "NVDA 180 put 2 张")

    assert reply.text.count(CLARIFICATION_NOTE) == 1
    assert reply.text.startswith("请补充张数。")
    # The replayed history must not carry the note into the next prompt.
    replayed = [item for item in model.calls[0]["messages"]
                if CLARIFICATION_NOTE in str(item.get("content") or "")]
    assert replayed == []
    # Nor is the note persisted, so it cannot be copied back into context.
    turns = conversations.context_turns(
        scope, NOW + timedelta(seconds=1), max_turns=10, max_tokens=100000,
        estimate=_estimate)
    assert all(CLARIFICATION_NOTE not in item["content"]
               for turn in turns if turn["turn_id"] != "legacy"
               for item in turn["messages"])


def test_legacy_tool_result_replays_as_context_not_assistant_prose(tmp_path):
    model = _ScriptedModel([_text_response("张数是多少？")])
    agent, conversations = _agent(tmp_path, model=model)
    scope = _event().scope_sha256("gq")
    legacy = json.dumps({"candidates": [{"contract_symbol": "NVDA270115P160000"}],
                         "displayed": 1, "omitted": 0})
    conversations.append_message(
        scope_sha256=scope, turn_id="legacy", seq=0, role="user",
        content="有什么候选", now=NOW, source_id="om_legacy",
        source_expiry=NOW + timedelta(hours=24))
    conversations.append_message(
        scope_sha256=scope, turn_id="legacy", seq=1, role="tool_result",
        content=legacy, now=NOW, source_id="om_legacy",
        source_expiry=NOW + timedelta(hours=24))
    conversations.append_message(
        scope_sha256=scope, turn_id="legacy", seq=2, role="assistant",
        content="有候选，见上。", now=NOW, source_id="om_legacy",
        source_expiry=NOW + timedelta(hours=24))

    agent.handle_event(_event(), "继续")

    messages = model.calls[0]["messages"]
    assert not any(item["role"] == "assistant" and "candidates" in
                   str(item.get("content") or "") for item in messages)
    context = [item for item in messages if item["role"] == "user"
               and str(item.get("content") or "").startswith(
                   "[历史查询结果，仅供指代，不要复述]")]
    assert len(context) == 1
    assert legacy in context[0]["content"]


def test_tool_result_history_replays_as_a_real_tool_exchange(tmp_path):
    model = _ScriptedModel([
        # The model may keep using an old market prefix; only the normalized
        # parameters are persisted and replayed.
        _tool_response("query_quotes", {"symbol": "US.NVDA"}),
        _text_response(""),
        _text_response("张数是多少？"),
    ])
    agent, _ = _agent(tmp_path, model=model)

    first = agent.handle_event(_event(message_id="om_1"), "NVDA 现价")
    agent.handle_event(_event(message_id="om_2"), "继续")

    messages = model.calls[-1]["messages"]
    calls = [item for item in messages
             if item["role"] == "assistant" and item.get("tool_calls")]
    assert [item["tool_calls"][0]["id"] for item in calls] == ["hist_0_1"]
    assert calls[0]["tool_calls"][0]["function"]["name"] == "query_quotes"
    replayed = json.loads(calls[0]["tool_calls"][0]["function"]["arguments"])
    assert replayed == {"symbol": "NVDA"}
    assert not any("US.NVDA" in json.dumps(item, ensure_ascii=False)
                   for item in messages)
    results = [item for item in messages
               if item["role"] == "tool" and item["tool_call_id"] == "hist_0_1"]
    assert len(results) == 1
    assert results[0]["content"].startswith("查询结果：实时行情")
    assert "185.0000" in results[0]["content"]
    assert not any("[历史查询结果" in str(item.get("content") or "")
                   for item in messages)
    # Result turns are not clarification turns: the disclaimer belongs to
    # clarification replies only.
    from om_group_query_bot.render import CLARIFICATION_NOTE
    assert CLARIFICATION_NOTE not in first.text


def test_optional_tool_parameters_declare_defaults():
    limit_defaults = []
    optional_descriptions = []

    def walk(schema):
        for name, prop in (schema.get("properties") or {}).items():
            if name == "limit":
                limit_defaults.append(prop)
            elif (name in {"dte", "strike", "abs_delta_range", "opened_at"}
                  and "description" in prop):
                optional_descriptions.append(prop["description"])
            elif isinstance(prop, dict) and "properties" in prop:
                walk(prop)

    for tool in TOOLS:
        walk(tool["input_schema"])
    assert len(limit_defaults) == 3
    assert all(field.get("default") == 10 for field in limit_defaults)
    assert all("可选" in text for text in optional_descriptions)
