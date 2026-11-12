"""Exercise authenticated message admission, persisted follow-ups and candidate replies."""
from datetime import timedelta
import json

import pytest

from om_group_query_bot.agent import BotService, TOOLS
from om_group_query_bot.config import load_bot_settings
from test_group_query_bot_agent import (
    CLOCK, NOW, _FakeAPI, _Replies, _ScriptedModel, _Secrets, _agent,
    _bot_config_file, _event, _payload, _settings, _text_response, _tool_response,
)


def _followup(message_id="om_followup", **kwargs):
    payload = _payload(message_id=message_id, **kwargs)
    payload["event"]["message"]["mentions"] = []
    payload["event"]["message"]["content"] = json.dumps({"text": "继续查询"})
    return payload


def _service_at(tmp_path, moment, *, responses=None, reply_fn=None, **overrides):
    model = _ScriptedModel(responses or [_text_response("还想了解什么？")] * 4)
    replies = reply_fn or _Replies()
    service = BotService(
        _settings(tmp_path, user_per_minute=30, **overrides),
        secret_provider=_Secrets(), request=model, reply_fn=replies,
        now=lambda: moment[0], clock=lambda: CLOCK,
    )
    return service, model, replies


def test_sdk_event_adapter_preserves_unmentioned_followups(tmp_path):
    from types import SimpleNamespace
    from src.infrastructure.feishu_ws_client import feishu_event_model_to_payload

    def sdk_model(value):
        if isinstance(value, dict):
            return SimpleNamespace(**{key: sdk_model(item) for key, item in value.items()})
        if isinstance(value, list):
            return [sdk_model(item) for item in value]
        return value

    moment = [NOW]
    service, model, replies = _service_at(tmp_path, moment)
    first = feishu_event_model_to_payload(sdk_model(_payload()))
    assert service.handle_payload(first, "ou_bot")["status"] == "acknowledged"
    followup = feishu_event_model_to_payload(sdk_model(_followup()))
    assert followup["event"]["message"]["mentions"] == []
    assert service.handle_payload(followup, "ou_bot")["status"] == "acknowledged"
    assert len(model.calls) == len(replies.calls) == 2
    assert replies.calls[-1]["message_id"] == "om_followup"


@pytest.mark.parametrize("elapsed,expected", [(599, "acknowledged"), (600, "rejected"), (650, "rejected")])
def test_window_is_measured_from_successfully_answered_message_receipt(tmp_path, elapsed, expected):
    moment = [NOW]
    transport = _Replies()

    def delayed_reply(**kwargs):
        moment[0] += timedelta(seconds=90)
        return transport(**kwargs)

    service, _, _ = _service_at(tmp_path, moment, reply_fn=delayed_reply)
    assert service.handle_payload(_payload(), "ou_bot")["status"] == "acknowledged"
    moment[0] = NOW + timedelta(seconds=elapsed)
    assert service.handle_payload(_followup(), "ou_bot")["status"] == expected


def test_first_message_needs_mention_then_followups_are_deduplicated(tmp_path):
    moment = [NOW]
    service, model, replies = _service_at(tmp_path, moment)
    assert service.handle_payload(_followup(), "ou_bot")["reason"] == "not_mentioned"
    assert model.calls == []
    assert service.deliveries.counts(NOW) == {}

    assert service.handle_payload(_payload(), "ou_bot")["status"] == "acknowledged"
    moment[0] += timedelta(minutes=1)
    assert service.handle_payload(_followup(), "ou_bot")["status"] == "acknowledged"
    assert service.handle_payload(_followup(), "ou_bot") == {
        "status": "duplicate", "delivery_state": "acknowledged",
    }
    assert len(model.calls) == len(replies.calls) == 2


def test_followup_window_survives_restart_and_successful_followups_renew_it(tmp_path):
    moment = [NOW]
    service, _, _ = _service_at(tmp_path, moment)
    service.handle_payload(_payload(), "ou_bot")
    moment[0] += timedelta(minutes=9)
    restarted, model, replies = _service_at(tmp_path, moment)
    assert restarted.handle_payload(_followup("om_2"), "ou_bot")["status"] == "acknowledged"
    moment[0] += timedelta(minutes=9)
    assert restarted.handle_payload(_followup("om_3"), "ou_bot")["status"] == "acknowledged"
    assert len(model.calls) == len(replies.calls) == 2


@pytest.mark.parametrize("window,elapsed", [(600, 600), (600, 601), (0, 1)])
def test_expired_or_disabled_followup_window_requires_a_new_mention(tmp_path, window, elapsed):
    moment = [NOW]
    service, model, replies = _service_at(
        tmp_path, moment, followup_window_seconds=window,
    )
    service.handle_payload(_payload(), "ou_bot")
    moment[0] += timedelta(seconds=elapsed)
    assert service.handle_payload(_followup(), "ou_bot")["reason"] == "not_mentioned"
    assert len(model.calls) == len(replies.calls) == 1
    assert service.handle_payload(_payload("om_new_mention"), "ou_bot")["status"] == "acknowledged"


@pytest.mark.parametrize("change", ["sender", "chat", "thread", "tenant", "other_mention"])
def test_followups_do_not_cross_identity_or_target_someone_else(tmp_path, change):
    moment = [NOW]
    service, model, replies = _service_at(
        tmp_path, moment, allowed_chat_ids=("oc_group", "oc_other"),
    )
    service.handle_payload(_payload(), "ou_bot")
    payload = _followup()
    if change == "sender":
        payload["event"]["sender"]["sender_id"]["open_id"] = "ou_bob"
    elif change == "chat":
        payload["event"]["message"]["chat_id"] = "oc_other"
    elif change == "thread":
        payload["event"]["message"]["thread_id"] = "thread_other"
    elif change == "tenant":
        payload["header"]["tenant_key"] = "tenant_other"
    else:
        payload["event"]["message"]["mentions"] = [{"id": {"open_id": "ou_someone_else"}}]
    assert service.handle_payload(payload, "ou_bot")["status"] == "rejected"
    assert len(model.calls) == len(replies.calls) == 1


@pytest.mark.parametrize("before_send", [False, True])
def test_unsent_or_unknown_reply_does_not_activate_followups(tmp_path, before_send):
    def failing_reply(**kwargs):
        if before_send:
            kwargs["before_send"]()
        raise RuntimeError("transport unavailable")

    moment = [NOW]
    service, model, _ = _service_at(tmp_path, moment, reply_fn=failing_reply)
    result = service.handle_payload(_payload(), "ou_bot")
    assert result["status"] == ("outcome_unknown" if before_send else "failed")
    assert service.conversations.counts(NOW)["messages"] > 0
    assert service.handle_payload(_followup(), "ou_bot")["reason"] == "not_mentioned"
    assert len(model.calls) == 1


@pytest.mark.parametrize("window", [0, 600, 3600])
def test_bot_config_reads_followup_window(tmp_path, window):
    path = _bot_config_file(tmp_path, ["oc_group"])
    raw = json.loads(path.read_text())
    raw["limits"] = {"followup_window_seconds": window}
    path.write_text(json.dumps(raw))
    assert load_bot_settings(path).followup_window_seconds == window


@pytest.mark.parametrize("window", [-1, 3601, True, "600", None])
def test_bot_config_rejects_invalid_followup_window(tmp_path, window):
    path = _bot_config_file(tmp_path, ["oc_group"])
    raw = json.loads(path.read_text())
    raw["limits"] = {"followup_window_seconds": window}
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="followup_window_seconds"):
        load_bot_settings(path)


def _candidate_data():
    return {
        "symbol": "0700.HK", "market": "HK", "outcome": "found",
        "candidates": [{
            "contract": "HK.TCH261120P375000", "expiry": "2026-11-20",
            "strike": 375.0, "dte": 8, "delta": -0.111,
            "evidence": {"bid": 1.34, "ask": 1.45},
            "metrics": {"annualized_net_return": 0.1452},
        }],
        "effective_filters": {
            "strike": None, "min_strike": None, "max_strike": 380.0,
            "strike_source": "request", "dte": [7, 60], "dte_source": "public_policy",
            "max_abs_delta": 0.4, "max_abs_delta_source": "public_policy", "limit": 10,
        },
        "displayed": 1, "omitted": 0, "reason_codes": [],
    }


def test_candidate_reply_uses_api_conditions_and_quotes_without_model_prose(tmp_path):
    response = _tool_response("query_candidates", {
        "symbol": "0700.HK", "strategy": "short_put", "max_strike": 380,
    })
    response["message"]["content"] = "已经按不超过500查询，买卖价不可用。"
    model = _ScriptedModel([response])
    api = _FakeAPI(data={"query_candidates": _candidate_data()})
    agent, _ = _agent(tmp_path, model=model, api=api)
    reply = agent.handle_event(_event(), "腾讯 max strike=380 的 short put")
    assert reply.status == "ok"
    assert len(model.calls) == len(api.calls) == 1
    assert "≤ 380.00" in reply.text
    assert "HK.TCH261120P375000" in reply.text and "2026-11-20" in reply.text
    assert "买 1.3400 / 卖 1.4500" in reply.text
    assert "14.52%" in reply.text
    assert "不超过500" not in reply.text and "不可用" not in reply.text


def test_candidate_refinement_receives_validated_current_parameters(tmp_path):
    args = {"symbol": "0700.HK", "strategy": "short_put", "strike": 380,
            "dte": [7, 60], "limit": 5}
    model = _ScriptedModel([
        _tool_response("query_candidates", args),
        _tool_response("query_candidates", {
            "symbol": "0700.HK", "strategy": "short_put", "max_strike": 380,
            "dte": [7, 60], "limit": 5,
        }),
    ])
    api = _FakeAPI(data={"query_candidates": _candidate_data()})
    agent, _ = _agent(tmp_path, model=model, api=api)
    agent.handle_event(_event(message_id="om_first"), "腾讯行权价380的short put")
    reply = agent.handle_event(_event(message_id="om_second"), "我指定的是max strike=380")
    system = model.calls[-1]["messages"][0]["content"]
    prefix = "不是本次查询结果；未指定的条件仍用公共策略默认）：\n"
    current = json.loads(system.split(prefix, 1)[1])
    assert current == {**args, "strike": 380.0}
    parameters = api.calls[-1]["parameters"]
    assert parameters["max_strike"] == 380
    assert "strike" not in parameters
    assert parameters["dte"] == [7, 60] and parameters["limit"] == 5
    assert api.calls[-1]["method"] == "query_candidates"
    assert "≤ 380.00" in reply.text


def test_four_turn_candidate_edits_work_with_and_without_mentions(tmp_path):
    from src.application.option_candidates import prepare_public_query

    class CandidateAPI(_FakeAPI):
        def call(self, **kwargs):
            self.calls.append(kwargs)
            prepared = prepare_public_query(
                {**kwargs["parameters"], "market": "HK"},
                {"sell_put": {"min_dte": 7, "max_dte": 60, "max_abs_delta": 0.4}},
                {"market_data_provider": "opend", "host": "127.0.0.1", "port": 11111},
                NOW,
            )
            return {**self._default("query_candidates"), "symbol": "0700.HK",
                    "market": "HK", "effective_filters": prepared.filters}

    queries = [
        {"max_strike": 380}, {"max_strike": 370},
        {"strike": 380}, {"max_strike": 380},
    ]
    texts = [
        "腾讯有max strike=380的short put吗？", "上限改成370",
        "改成精确行权价380", "我指定的是max strike=380",
    ]
    responses = [_tool_response("query_candidates", {
        "symbol": "0700.HK", "strategy": "short_put", "dte": [7, 60], **query,
    }) for query in queries]
    moment = [NOW]
    service, model, replies = _service_at(tmp_path, moment, responses=responses)
    api = CandidateAPI()
    service.agent.api_client = api
    for index, text in enumerate(texts):
        payload = (_payload(f"om_{index}") if index % 2 == 0
                   else _followup(f"om_{index}"))
        payload["event"]["message"]["content"] = json.dumps({"text": text})
        assert service.handle_payload(payload, "ou_bot")["status"] == "acknowledged"
        moment[0] += timedelta(seconds=60)
    assert len(model.calls) == len(api.calls) == len(replies.calls) == 4
    for call, expected in zip(api.calls, queries):
        assert call["method"] == "query_candidates"
        actual = call["parameters"]
        assert {key: actual[key] for key in ("strike", "min_strike", "max_strike")
                if key in actual} == expected
        assert actual["symbol"] == "0700.HK" and actual["strategy"] == "short_put"
        assert actual["dte"] == [7, 60]
    assert "≤ 380.00" in replies.calls[0]["text"]
    assert "≤ 370.00" in replies.calls[1]["text"]
    assert "行权价 380.00" in replies.calls[2]["text"]
    assert "≤ 380.00" in replies.calls[3]["text"]
    system = model.calls[-1]["messages"][0]["content"]
    prefix = "不是本次查询结果；未指定的条件仍用公共策略默认）：\n"
    previous = json.loads(system.split(prefix, 1)[1])
    assert previous["strike"] == 380 and "max_strike" not in previous


def test_candidate_schema_exposes_three_inclusive_strike_fields():
    tool = next(tool for tool in TOOLS if tool["name"] == "query_candidates")
    properties = tool["input_schema"]["properties"]
    for name in ("strike", "min_strike", "max_strike"):
        assert properties[name]["type"] == "number"
        assert properties[name]["exclusiveMinimum"] == 0
    assert "含" in properties["min_strike"]["description"]
    assert "含" in properties["max_strike"]["description"]
    assert not any("inclusive" in name for name in properties)
