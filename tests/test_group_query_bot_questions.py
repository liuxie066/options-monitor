"""Host questions, delivery-qualified choices and source-bounded continuation."""
from datetime import timedelta, datetime
import json

import pytest

from om_group_query_bot.agent import BotService, MODEL_TOOLS, TOOL_NAMES
from om_group_query_bot.store import ConversationStore
from test_group_query_bot_agent import (
    CLOCK, NOW, _FakeAPI, _Replies, _ScriptedModel, _Secrets, _agent,
    _event, _payload, _settings, _text_response, _tool_response, _roll_arguments,
)


def _ask(question="你想查行情还是找卖出期权候选？", options=None):
    return _tool_response("ask_user_question", {"question": question,
                                                "options": options or []})


def _service(tmp_path, moment, responses, replies=None):
    model = _ScriptedModel(responses)
    service = BotService(_settings(tmp_path, user_per_minute=30),
                         secret_provider=_Secrets(), request=model,
                         reply_fn=replies or _Replies(), now=lambda: moment[0],
                         clock=lambda: CLOCK)
    service.agent._estimate = lambda _: 100
    api = _FakeAPI()
    service.agent.api_client = api
    return service, model, api


def _send(service, message_id, text):
    return service.handle_payload(_payload(message_id), "ou_bot", text=text)


def test_question_is_host_only_and_direct_followup_uses_canonical_symbol(tmp_path):
    assert "ask_user_question" not in TOOL_NAMES
    assert "ask_user_question" in {tool["name"] for tool in MODEL_TOOLS}
    moment = [NOW]
    service, model, api = _service(tmp_path, moment, [
        _ask(options=["行情", "卖出 Put"]),
        _tool_response("query_candidates", {"symbol": "nflx", "strategy": "short_put"}),
    ])
    assert _send(service, "om_a", "分析下 nflx")["agent_status"] == "clarification"
    assert api.calls == []
    assert "1. 行情" in service.conversations.context_turns(
        _event().scope_sha256("gq"), NOW, max_turns=10, max_tokens=4000,
        estimate=lambda _: 1)[0]["messages"][-1]["content"]
    assert _send(service, "om_b", "2")["status"] == "acknowledged"
    assert api.calls[0]["parameters"] == {"symbol": "NFLX", "strategy": "short_put", "limit": 10}
    assert "卖出 Put" in str(model.calls[-1]["messages"])


@pytest.mark.parametrize("reverse", [False, True])
def test_question_and_business_in_same_batch_never_call_api(tmp_path, reverse):
    question = _ask()["message"]["tool_calls"][0]
    business = _tool_response("query_quotes", {"symbol": "NFLX"}, "call_2")["message"]["tool_calls"][0]
    batch = [question, business]
    if reverse:
        batch.reverse()
    agent, _ = _agent(tmp_path, model=_ScriptedModel([
        {"message": {"tool_calls": batch}}, _ask(),
    ]))
    reply = agent.handle_event(_event(), "分析 nflx")
    assert agent.api_client.calls == []
    assert reply.status == "clarification"


@pytest.mark.parametrize("args", [
    {"question": ""}, {"question": "x" * 301},
    {"question": "确认？", "options": [" a", "a "]},
    {"question": "确认？", "options": [1]},
    {"question": "确认？", "options": ["a", "b", "c", "d"]},
    {"question": "确认？", "request_context": {"chat_id": "other"}},
])
def test_invalid_question_is_not_rendered_or_executed(tmp_path, args):
    agent, _ = _agent(tmp_path, model=_ScriptedModel([
        _tool_response("ask_user_question", args), _text_response(""),
    ]))
    reply = agent.handle_event(_event(), "分析下 NFLX")
    assert reply.status == "clarification"
    assert "你想查行情" in reply.text
    assert agent.api_client.calls == []


@pytest.mark.parametrize("arguments,expected,absent", [
    ({"symbol": "NFLX"}, "卖出 Put", "合约张数"),
    ({"symbol": "NFLX", "strategy": "invalid"}, "卖出 Put", "开仓权利金"),
    ({"symbol": "NFLX", "strategy": "short_put", "strike": 80, "max_strike": 90},
     "行权价", "开仓权利金"),
])
def test_noncooperative_model_gets_field_specific_host_question(tmp_path, arguments, expected, absent):
    agent, _ = _agent(tmp_path, model=_ScriptedModel([
        _tool_response("query_candidates", arguments), _text_response("我不知道"),
    ]))
    reply = agent.handle_event(_event(), "找候选")
    assert reply.status == "clarification"
    assert expected in reply.text and absent not in reply.text
    assert agent.api_client.calls == []


@pytest.mark.parametrize("state", ["failed", "outcome_unknown", "missing"])
@pytest.mark.parametrize("restart", [False, True])
def test_choice_never_uses_a_newer_unacknowledged_question(tmp_path, state, restart):
    moment = [NOW]
    transport = _Replies()
    count = [0]

    def reply(**kwargs):
        count[0] += 1
        if count[0] == 2 and state != "missing":
            if state == "outcome_unknown":
                kwargs["before_send"]()
            raise RuntimeError("injected")
        return transport(**kwargs)

    service, _, api = _service(tmp_path, moment, [
        _ask("原问题", ["行情", "卖出 Put"]),
        _ask("新问题", ["卖出 Call", "行情"]),
        _tool_response("query_candidates", {"symbol": "NFLX", "strategy": "short_call"}),
    ], reply)
    _send(service, "om_a", "分析 NFLX")
    moment[0] += timedelta(seconds=10)
    _send(service, "om_b", "再确认一下")
    if state == "missing":
        with service.deliveries.connect() as db:
            db.execute("DELETE FROM deliveries WHERE message_id = 'om_b'")
    if restart:
        service, model, api = _service(tmp_path, moment, [
            _tool_response("query_candidates", {"symbol": "NFLX", "strategy": "short_call"}),
        ])
    # A still admits unmentioned followups, independently of B's evidence.
    moment[0] += timedelta(seconds=1)
    payload = _payload("om_c")
    payload["event"]["message"]["mentions"] = []
    result = service.handle_payload(payload, "ou_bot", text="1")
    assert result["status"] == "acknowledged"
    assert result["agent_status"] == "clarification"
    assert api.calls == []
    history = service.conversations.context_turns(
        _event().scope_sha256("gq"), moment[0], max_turns=10,
        max_tokens=4000, estimate=lambda _: 1)
    from om_group_query_bot.agent import _matching_history
    filtered, _ = _matching_history(history[:2], "自由回答")
    assert "新问题" not in str(filtered)
    assert "再确认一下" in str(filtered)  # Preserve B's valid user input.


@pytest.mark.parametrize("restart", [False, True])
def test_direct_question_without_pending_never_renews_inherited_sources(tmp_path, restart):
    moment = [NOW]
    args = _roll_arguments()
    responses = [_ask("最晚新到期日是哪天？"), _tool_response("evaluate_roll_down", args),
                 _text_response("")]
    service, model, api = _service(tmp_path, moment, responses)
    _send(service, "om_a", "NVDA 2026-12-18 Put 180 空头2张，向下移仓")
    scope = _event().scope_sha256("gq")
    assert service.conversations.pending_inputs(scope, NOW) == []
    moment[0] += timedelta(hours=23)
    if restart:
        service, model, api = _service(tmp_path, moment, responses[1:])
    _send(service, "om_b", "最晚到2027-01-15")
    assert len(api.calls) == 1
    expiry = NOW + timedelta(hours=24)
    with service.conversations.connect() as db:
        for table in ["scenario_inputs", "messages", "outbox_payloads"]:
            rows = db.execute(f"SELECT * FROM {table} WHERE source_id = 'om_b'").fetchall()
            assert rows
            for row in rows:
                if table == "messages" and row["role"] == "user":
                    assert datetime.fromisoformat(row["expires_at_utc"]) == moment[0] + timedelta(hours=24)
                else:
                    assert datetime.fromisoformat(row["expires_at_utc"]) <= expiry
                    assert datetime.fromisoformat(row["source_expires_at_utc"]) <= expiry


@pytest.mark.parametrize("elapsed", [24, 25])
def test_at_exact_expiry_and_after_restart_old_facts_are_not_provided(tmp_path, elapsed):
    moment = [NOW]
    service, _, _ = _service(tmp_path, moment, [_ask("最晚新到期日？")])
    _send(service, "om_a", "原始合约 NVDA Put 180 空头2张")
    moment[0] += timedelta(hours=elapsed)
    service, model, api = _service(tmp_path, moment, [_ask("请重新确认原合约和张数。")])
    _send(service, "om_b", "最晚到2027-01-15")
    assert "原始合约" not in str(model.calls[0]["messages"])
    assert api.calls == []


def test_explicit_new_task_does_not_inherit_irrelevant_expiry(tmp_path):
    moment = [NOW]
    service, _, api = _service(tmp_path, moment, [
        _ask("最晚新到期日？"),
        _tool_response("query_candidates", {"symbol": "NFLX", "strategy": "short_put"}),
    ])
    _send(service, "om_a", "旧 NVDA 合约")
    moment[0] += timedelta(hours=23)
    _send(service, "om_b", "新任务：查 NFLX sell put")
    with service.conversations.connect() as db:
        rows = db.execute("SELECT expires_at_utc FROM messages WHERE source_id = 'om_b'").fetchall()
    assert all(datetime.fromisoformat(row[0]) == moment[0] + timedelta(hours=24) for row in rows)
    assert api.calls[0]["parameters"]["symbol"] == "NFLX"


def test_cancel_never_queries_even_with_noncooperative_model(tmp_path):
    model = _ScriptedModel([_tool_response("query_quotes", {"symbol": "NFLX"})])
    agent, _ = _agent(tmp_path, model=model)
    assert agent.handle_event(_event(), "先不查了").status == "cancelled"
    assert agent.api_client.calls == model.calls == []


def test_cancel_single_filter_preserves_task_and_queries_again(tmp_path):
    responses = [
        _tool_response("query_candidates", {"symbol": "NFLX", "strategy": "short_put", "max_strike": 90}),
        _tool_response("query_candidates", {"symbol": "NFLX", "strategy": "short_put"}),
    ]
    moment = [NOW]
    service, model, api = _service(tmp_path, moment, responses)
    _send(service, "om_a", "NFLX sell put 最高90")
    moment[0] += timedelta(seconds=1)
    _send(service, "om_b", "取消行权价上限")
    assert len(api.calls) == 2
    assert "max_strike" not in api.calls[-1]["parameters"]
    assert "NFLX" in str(model.calls[-1]["messages"])


def test_only_latest_independent_boundary_contributes_to_choices_and_expiry(tmp_path):
    moment = [NOW]
    service, model, api = _service(tmp_path, moment, [
        _ask("旧问题A", ["Put", "Call"]), _ask("新问题B", ["行情", "候选"]),
        _tool_response("query_quotes", {"symbol": "NFLX"}), _text_response(""),
    ])
    _send(service, "om_a", "新任务：旧NVDA")
    moment[0] += timedelta(hours=23)
    _send(service, "om_b", "新任务：NFLX")
    moment[0] += timedelta(seconds=1)
    result = _send(service, "om_c", "1")
    assert result["agent_status"] == "ok"
    assert len(api.calls) == 1
    assert "旧问题A" not in str(model.calls[-1]["messages"])
    with service.conversations.connect() as db:
        row = db.execute("SELECT expires_at_utc FROM messages WHERE source_id = 'om_c' AND role='assistant'").fetchone()
    assert datetime.fromisoformat(row[0]) == NOW + timedelta(hours=47)


def test_fresh_pending_has_own_lifetime_even_with_old_same_operation(tmp_path):
    moment = [NOW]
    service, _, _ = _service(tmp_path, moment, [
        _tool_response("evaluate_roll_down", {"position": {}}), _ask("原合约？"),
        _tool_response("evaluate_roll_down", {"position": {}}), _ask("新合约？"),
    ])
    _send(service, "om_a", "旧移仓任务")
    moment[0] += timedelta(hours=23)
    _send(service, "om_b", "新任务：移仓")
    with service.conversations.connect() as db:
        rows = db.execute("SELECT expires_at_utc FROM scenario_inputs WHERE source_id = 'om_b'").fetchall()
    assert rows and all(datetime.fromisoformat(row[0]) == NOW + timedelta(hours=47) for row in rows)


@pytest.mark.parametrize("audit", [False, True])
def test_legacy_or_missing_audit_cannot_leak_undelivered_question(tmp_path, audit):
    moment = [NOW]
    service, model, _ = _service(tmp_path, moment, [_ask("请确认你的目标。")])
    scope = _event().scope_sha256("gq")
    for seq, role, content in [(0, "user", "有效NFLX用户输入"),
                               (1, "assistant", "未送达的旧问题：1. 买入 2. 卖出")]:
        service.conversations.append_message(scope_sha256=scope, turn_id="old", seq=seq,
            role=role, content=content, now=NOW, source_id="old",
            source_expiry=NOW + timedelta(hours=24))
    if audit:
        service.conversations.record_audit(scope_sha256=scope, turn_id="old", status="ok",
                                          reason_codes=(), now=NOW)
    moment[0] += timedelta(seconds=1)
    _send(service, "om_new", "继续")
    context = str(model.calls[0]["messages"])
    assert "未送达的旧问题" not in context
    assert "有效NFLX用户输入" in context


@pytest.mark.parametrize("code", ["DEPENDENCY_UNAVAILABLE", "PERMISSION_DENIED"])
def test_query_failure_cannot_be_disguised_as_a_question(tmp_path, code):
    from om_group_query_bot.api_client import QueryAPIError
    api = _FakeAPI(error=QueryAPIError(code, "injected failure"))
    agent, _ = _agent(tmp_path, model=_ScriptedModel([
        _tool_response("query_quotes", {"symbol": "NFLX"}), _ask("你想买还是卖？"),
    ]), api=api)
    reply = agent.handle_event(_event(), "NFLX报价")
    assert len(api.calls) == 1
    assert reply.status == "query_failed" and code in reply.reason_codes
    assert "买还是卖" not in reply.text
    assert ("权限不足" if code == "PERMISSION_DENIED" else "当前不可用") in reply.text


@pytest.mark.parametrize("restart", [False, True])
def test_close_premium_single_field_reply_keeps_direct_question_source(tmp_path, restart):
    moment = [NOW]
    args = {"position": {"symbol": "NVDA", "contract": {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180},
        "side": "short", "contracts": 2},
        "opening_premium": {"amount": 4.2, "unit": "per_share", "currency": "USD"}}
    responses = [_ask("开仓权利金是多少？请说明单位和币种。"),
                 _tool_response("evaluate_close", args), _text_response("")]
    service, _, api = _service(tmp_path, moment, responses)
    _send(service, "om_a", "评估NVDA 2026-12-18 Put 180 空头2张的平仓")
    assert service.conversations.pending_inputs(_event().scope_sha256("gq"), NOW) == []
    moment[0] += timedelta(hours=23)
    if restart:
        service, _, api = _service(tmp_path, moment, responses[1:])
    _send(service, "om_b", "每股4.2美元")
    assert api.calls[0]["parameters"] == args
    with service.conversations.connect() as db:
        for table in ["scenario_inputs", "messages", "outbox_payloads"]:
            rows = db.execute(f"SELECT * FROM {table} WHERE source_id='om_b'").fetchall()
            assert rows
            for row in rows:
                if table == "messages" and row["role"] == "user":
                    continue
                assert datetime.fromisoformat(row["expires_at_utc"]) <= NOW + timedelta(hours=24)


def test_missing_source_metadata_blocks_even_a_model_ready_to_query(tmp_path, monkeypatch):
    model = _ScriptedModel([_tool_response("query_quotes", {"symbol": "NFLX"})])
    agent, store = _agent(tmp_path, model=model)
    monkeypatch.setattr(store, "context_turns", lambda *a, **kw: [
        {"messages": [{"role": "user", "content": "NFLX"}]}])
    reply = agent.handle_event(_event(), "继续")
    assert reply.status == "clarification"
    assert model.calls == agent.api_client.calls == []


def test_new_pending_then_confirmation_does_not_rebind_an_old_identical_input(tmp_path):
    moment = [NOW]
    args = _roll_arguments()
    service, _, api = _service(tmp_path, moment, [
        _tool_response("evaluate_roll_down", args), _text_response(""),
        _tool_response("evaluate_roll_down", {"position": args["position"]}), _ask("最晚日期？"),
        _tool_response("evaluate_roll_down", args), _text_response(""),
    ])
    _send(service, "om_a", "旧移仓任务完整输入")
    old_id = api.calls[0]["input_id"]
    moment[0] += timedelta(hours=23)
    _send(service, "om_b", "新任务：移仓NVDA 2026-12-18 Put 180 空头2张")
    moment[0] += timedelta(seconds=1)
    _send(service, "om_c", "最晚到2027-01-15")
    assert api.calls[-1]["input_id"] != old_id
    with service.conversations.connect() as db:
        row = db.execute("SELECT expires_at_utc FROM scenario_inputs WHERE input_id=?",
                         (api.calls[-1]["input_id"],)).fetchone()
    assert datetime.fromisoformat(row[0]) == NOW + timedelta(hours=47)


def test_source_expiring_during_model_call_blocks_business_and_fact_delivery(tmp_path):
    moment = [NOW]
    service, _, api = _service(tmp_path, moment, [_ask("权利金？")])
    _send(service, "om_a", "NVDA旧合约事实")
    moment[0] += timedelta(hours=24, seconds=-1)

    def delayed_model(**kwargs):
        moment[0] += timedelta(seconds=1)
        return _tool_response("query_quotes", {"symbol": "NVDA"})

    service.agent._request = delayed_model
    result = _send(service, "om_b", "继续查")
    assert result["agent_status"] == "clarification"
    assert api.calls == []
    with service.conversations.connect() as db:
        rows = db.execute("SELECT content FROM messages WHERE source_id='om_b' AND role='assistant'").fetchall()
    assert rows and "已到期" in rows[0][0]


@pytest.mark.parametrize("answer", ["4", "1。", "我选第一个"])
def test_ambiguous_number_variants_are_not_given_to_model(tmp_path, answer):
    moment = [NOW]
    service, model, api = _service(tmp_path, moment, [
        _ask("选择？", ["行情", "候选"]), _ask("再选择？", ["Put", "Call"]),
        _tool_response("query_quotes", {"symbol": "NFLX"}),
    ])
    _send(service, "om_a", "NFLX分析")
    moment[0] += timedelta(seconds=1)
    _send(service, "om_b", "再问一遍")
    moment[0] += timedelta(seconds=1)
    assert _send(service, "om_c", answer)["agent_status"] == "clarification"
    assert len(model.calls) == 2 and api.calls == []


@pytest.mark.parametrize("field,value,expected", [
    ("strategy", [], "卖出 Put"), ("strategy", {}, "卖出 Put"),
])
def test_invalid_enum_types_return_targeted_question(tmp_path, field, value, expected):
    agent, _ = _agent(tmp_path, model=_ScriptedModel([
        _tool_response("query_candidates", {"symbol": "NFLX", field: value}),
        _text_response(""),
    ]))
    reply = agent.handle_event(_event(), "NFLX候选")
    assert reply.status == "clarification"
    assert expected in reply.text
    assert agent.api_client.calls == []


def test_source_expiring_just_before_transport_does_not_send_inherited_reply(tmp_path):
    moment = [NOW]
    sent = []

    def transport(**kwargs):
        if kwargs["message_id"] == "om_b":
            moment[0] = NOW + timedelta(hours=24)
        kwargs["before_send"]()
        sent.append(kwargs["text"])
        return {"code": 0, "data": {"message_id": "reply_" + kwargs["message_id"]}}

    service, _, _ = _service(tmp_path, moment, [_ask("方向？"), _ask("张数？")], transport)
    _send(service, "om_a", "NVDA旧合约")
    moment[0] += timedelta(hours=23)
    assert _send(service, "om_b", "Put")["status"] == "failed"
    assert len(sent) == 1


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("boundary", ["新任务：查NFLX sell put", "先不查了"])
def test_retired_pending_never_revives_after_boundary_is_cropped(tmp_path, restart, boundary):
    moment = [NOW]
    model = _ScriptedModel([
        _tool_response("evaluate_roll_down", {"position": {"symbol": "NVDA"}}),
        _ask("原合约？"),
        *[_tool_response("answer_general", {"answer": "你好。"})] * 4,
    ])
    service = BotService(_settings(tmp_path, context_turns=1, user_per_minute=30),
                         secret_provider=_Secrets(), request=model, reply_fn=_Replies(),
                         now=lambda: moment[0], clock=lambda: CLOCK)
    service.agent.api_client = _FakeAPI()
    _send(service, "om_a", "旧移仓任务 NVDA")
    scope = _event().scope_sha256("gq")
    assert service.conversations.pending_inputs(scope, moment[0])
    moment[0] += timedelta(seconds=1)
    _send(service, "om_b", boundary)
    assert service.conversations.pending_inputs(scope, moment[0]) == []
    if restart:
        service = BotService(_settings(tmp_path, context_turns=1, user_per_minute=30),
                             secret_provider=_Secrets(), request=model, reply_fn=_Replies(),
                             now=lambda: moment[0], clock=lambda: CLOCK)
        service.agent.api_client = _FakeAPI()
    for index in range(2):
        moment[0] += timedelta(seconds=1)
        _send(service, f"om_next_{index}", "你好")
    assert "待补参数" not in model.calls[-1]["messages"][0]["content"]
    assert "NVDA" not in str(model.calls[-1]["messages"][1:])
