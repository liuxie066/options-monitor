"""Current-message reconstruction proves a new source without business probing."""
from datetime import datetime, timedelta
import json

import pytest

from om_group_query_bot.agent import BotService, MODEL_TOOLS, TOOLS
from test_group_query_bot_agent import (
    CLOCK, NOW, _FakeAPI, _Replies, _Secrets, _ScriptedModel, _settings,
    _text_response, _tool_response, _event,
)
from test_group_query_bot_questions import _ask, _send, _service

CLOSE_ARGS = {
    "position": {"symbol": "NVDA", "contract": {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180},
        "side": "short", "contracts": 2},
    "opening_premium": {"amount": 4.2, "unit": "per_share", "currency": "USD"},
}
COMPLETE = "我再完整说一遍：评估 NVDA 2026-12-18 Put 180 空头2张，开仓每股4.2美元"


def _fresh_call(name="evaluate_close", args=None, *, omitted=False):
    response = _tool_response(name, {**(CLOSE_ARGS if args is None else args),
                                     "source_scope": "current_message"})
    if omitted:
        function = response["message"]["tool_calls"][0]["function"]
        arguments = json.loads(function["arguments"])
        arguments.pop("source_scope")
        function["arguments"] = json.dumps(arguments)
    return response


def _assert_expiry(service, source_id, expected):
    with service.conversations.connect() as db:
        for table in ("scenario_inputs", "messages", "outbox_payloads"):
            rows = db.execute(f"SELECT * FROM {table} WHERE source_id = ?", (source_id,)).fetchall()
            assert rows
            for row in rows:
                assert datetime.fromisoformat(row["expires_at_utc"]) == expected
                assert datetime.fromisoformat(row["source_expires_at_utc"]) == expected


@pytest.mark.parametrize("omitted", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_full_natural_restatement_is_a_new_source(tmp_path, omitted, restart):
    moment = [NOW]
    responses = [_ask("权利金金额、单位和币种？"), _fresh_call(omitted=omitted),
                 _fresh_call(args={**CLOSE_ARGS, "position": {
                     **CLOSE_ARGS["position"], "symbol": "US.NVDA"}}), _text_response("")]
    service, model, api = _service(tmp_path, moment, responses)
    _send(service, "om_a", "旧原文 NVDA 2026-12-18 Put 180 空头2张")
    moment[0] += timedelta(hours=23)
    if restart:
        service, model, api = _service(tmp_path, moment, responses[1:])
    assert _send(service, "om_b", COMPLETE)["agent_status"] == "ok"
    assert len(api.calls) == 1
    assert api.calls[0]["parameters"] == CLOSE_ARGS
    _assert_expiry(service, "om_b", moment[0] + timedelta(hours=24))
    rebuild_calls = [call for call in model.calls if "独立来源核验" in call["messages"][0]["content"]]
    assert len(rebuild_calls) == 1
    assert rebuild_calls[0]["messages"][1:] == [{"role": "user", "content": COMPLETE}]
    assert "旧原文" not in str(rebuild_calls[0]["messages"])
    assert "待补参数" not in rebuild_calls[0]["messages"][0]["content"]
    # The final narration is also rebuilt from the new source and fresh API data.
    assert "旧原文" not in str(model.calls[-1]["messages"])
    history = service.conversations.context_turns(
        _event().scope_sha256("gq"), moment[0], max_turns=10,
        max_tokens=10000, estimate=lambda _: 1)
    assert history[-1]["current_message_source"]


def test_new_source_boundary_survives_restart_and_future_partial_followup(tmp_path):
    moment = [NOW]
    service, _, api = _service(tmp_path, moment, [
        _ask("权利金？"), _fresh_call(), _fresh_call(), _text_response(""),
    ])
    _send(service, "om_a", "旧合约事实 NVDA 空头2张")
    moment[0] += timedelta(hours=23)
    _send(service, "om_b", COMPLETE)
    moment[0] += timedelta(minutes=30)
    service, model, api = _service(tmp_path, moment, [
        _tool_response("evaluate_close", {**CLOSE_ARGS, "position": {
            **CLOSE_ARGS["position"], "contracts": 3}}), _text_response(""),
    ])
    _send(service, "om_c", "张数改成3")
    assert len(api.calls) == 1 and api.calls[0]["parameters"]["position"]["contracts"] == 3
    assert "旧合约事实" not in str(model.calls[0]["messages"])
    with service.conversations.connect() as db:
        rows = db.execute("SELECT expires_at_utc FROM messages WHERE source_id='om_c' AND role!='user'").fetchall()
    assert rows and all(datetime.fromisoformat(row[0]) == NOW + timedelta(hours=47) for row in rows)


def test_single_premium_supplement_does_not_probe_or_renew(tmp_path):
    moment = [NOW]
    service, model, api = _service(tmp_path, moment, [
        _ask("权利金？"), _tool_response("evaluate_close", CLOSE_ARGS), _text_response(""),
    ])
    _send(service, "om_a", "NVDA 2026-12-18 Put 180 空头2张")
    moment[0] += timedelta(hours=23)
    _send(service, "om_b", "每股4.2美元")
    assert len(api.calls) == 1
    assert not any("独立来源核验" in call["messages"][0]["content"] for call in model.calls)
    with service.conversations.connect() as db:
        row = db.execute("SELECT expires_at_utc FROM scenario_inputs WHERE source_id='om_b'").fetchone()
    assert datetime.fromisoformat(row[0]) == NOW + timedelta(hours=24)
    assert "source_scope" not in api.calls[0]["parameters"]


@pytest.mark.parametrize("probe", [
    _ask("缺少必要事实？"),
    _fresh_call(args={"position": CLOSE_ARGS["position"]}),
    _fresh_call(args={**CLOSE_ARGS, "opening_premium": {
        "amount": 99, "unit": "per_share", "currency": "USD"}}),
    _tool_response("query_quotes", {"symbol": "NFLX"}),
    _text_response("self_contained=true"),
    RuntimeError("model unavailable"),
    {"message": {"tool_calls": []}, "finish_reason": "length"},
])
def test_uncertain_reconstruction_requires_confirmation_without_io_or_renewal(tmp_path, probe):
    moment = [NOW]
    service, model, api = _service(tmp_path, moment, [_ask("权利金？"), _fresh_call(), probe])
    _send(service, "om_a", "NVDA旧合约事实")
    moment[0] += timedelta(hours=23)
    result = _send(service, "om_b", COMPLETE)
    assert result["agent_status"] == "clarification"
    assert api.calls == []
    with service.conversations.connect() as db:
        assert db.execute("SELECT count(*) FROM scenario_inputs WHERE source_id='om_b'").fetchone()[0] == 0
        row = db.execute("SELECT expires_at_utc FROM outbox_payloads WHERE source_id='om_b'").fetchone()
    assert datetime.fromisoformat(row[0]) <= NOW + timedelta(hours=24)
    assert len(model.calls) == 3  # At most one independent probe, including failures.


def test_probe_cannot_extend_ttl_by_supplying_host_fields(tmp_path):
    moment = [NOW]
    malicious = _fresh_call(args={**CLOSE_ARGS, "source_expires_at_utc": "2099-01-01T00:00:00Z"})
    service, _, api = _service(tmp_path, moment, [_ask("权利金？"), _fresh_call(), malicious])
    _send(service, "om_a", "NVDA旧事实")
    moment[0] += timedelta(hours=23)
    assert _send(service, "om_b", COMPLETE)["agent_status"] == "clarification"
    assert api.calls == []


@pytest.mark.parametrize("limit", [1, 2])
def test_source_check_consumes_existing_call_budget(tmp_path, limit):
    moment = [NOW]
    model = _ScriptedModel([_ask("权利金？"), _fresh_call(), _fresh_call(), _text_response("")])
    service = BotService(_settings(tmp_path, model_calls=limit, user_per_minute=30),
                         secret_provider=_Secrets(), request=model, reply_fn=_Replies(),
                         now=lambda: moment[0], clock=lambda: CLOCK)
    service.agent._estimate = lambda _: 100
    api = service.agent.api_client = _FakeAPI()
    _send(service, "om_a", "NVDA旧事实")
    moment[0] += timedelta(hours=23)
    result = _send(service, "om_b", COMPLETE)
    assert len(model.calls) <= 1 + limit
    if limit == 1:
        assert result["agent_status"] == "clarification"
        assert api.calls == []
    else:
        # Extraction + one source probe exhaust the two-call budget;
        # fresh business facts may be rendered with the existing budget notice.
        assert result["agent_status"] == "limited"
        assert len(api.calls) == 1


def test_first_turn_and_explicit_independent_task_need_no_probe(tmp_path):
    moment = [NOW]
    args = {"symbol": "NFLX", "strategy": "short_put"}
    service, model, api = _service(tmp_path, moment, [
        _fresh_call("query_candidates", args), _fresh_call("query_candidates", args),
    ])
    _send(service, "om_a", "NFLX sell put")
    moment[0] += timedelta(hours=23)
    _send(service, "om_b", "新任务：NFLX sell put")
    assert len(model.calls) == len(api.calls) == 2
    assert all("独立来源核验" not in call["messages"][0]["content"] for call in model.calls)


def test_source_hint_is_host_only_and_does_not_grant_renewal_by_itself():
    assert all("source_scope" not in tool["input_schema"]["properties"] for tool in TOOLS)
    assert all("source_scope" in tool["input_schema"]["properties"]
               for tool in MODEL_TOOLS if tool["name"] in {item["name"] for item in TOOLS})


def test_source_probe_deadline_failure_performs_no_business_io(tmp_path):
    moment, monotonic = [NOW], [CLOCK]
    service, _, api = _service(tmp_path, moment, [_ask("权利金？")])
    _send(service, "om_a", "NVDA旧合约事实")
    moment[0] += timedelta(hours=23)
    service.agent._clock = lambda: monotonic[0]
    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        if "独立来源核验" in kwargs["messages"][0]["content"]:
            assert api.calls == []
            monotonic[0] += 91
        return _fresh_call()

    service.agent._request = request
    assert _send(service, "om_b", COMPLETE)["agent_status"] == "clarification"
    assert len(calls) == 2 and api.calls == []


def test_source_probe_reserves_tokens_from_same_turn_pool(tmp_path):
    moment = [NOW]
    model = _ScriptedModel([_ask("权利金？"), _fresh_call(), _fresh_call()])
    service = BotService(_settings(tmp_path, reserve_tokens=1500, output_tokens=1000,
                                   user_per_minute=30), secret_provider=_Secrets(),
                         request=model, reply_fn=_Replies(), now=lambda: moment[0],
                         clock=lambda: CLOCK)
    service.agent._estimate = lambda _: 100
    api = service.agent.api_client = _FakeAPI()
    _send(service, "om_a", "NVDA旧事实")
    moment[0] += timedelta(hours=23)
    assert _send(service, "om_b", COMPLETE)["agent_status"] == "clarification"
    assert len(model.calls) == 2 and api.calls == []


def test_source_probe_mixed_tools_cannot_probe_business(tmp_path):
    probe = _fresh_call()
    probe["message"]["tool_calls"] += _ask()["message"]["tool_calls"]
    moment = [NOW]
    service, _, api = _service(tmp_path, moment, [_ask("权利金？"), _fresh_call(), probe])
    _send(service, "om_a", "NVDA旧事实")
    moment[0] += timedelta(hours=23)
    assert _send(service, "om_b", COMPLETE)["agent_status"] == "clarification"
    assert api.calls == []


def test_probe_excludes_pending_and_old_tool_results(tmp_path):
    moment = [NOW]
    service, model, api = _service(tmp_path, moment, [
        _tool_response("query_candidates", {"symbol": "NFLX", "strategy": "short_put"}),
        _fresh_call(), _fresh_call(), _text_response(""),
    ])
    api._data["query_candidates"] = {**api._default("query_candidates"),
                                     "symbol": "NFLX", "old_only_marker": "private-old-result"}
    _send(service, "om_a", "旧候选查询NFLX，旧条件123.456")
    service.conversations.save_pending_input(
        scope_sha256=_event().scope_sha256("gq"), operation="evaluate_close",
        parameters={"old_pending_marker": "private-old-pending"}, now=NOW,
        source_id="om_a", source_expiry=NOW + timedelta(hours=24))
    moment[0] += timedelta(hours=23)
    _send(service, "om_b", COMPLETE)
    assert len(api.calls) == 2
    probe = next(call for call in model.calls if "独立来源核验" in call["messages"][0]["content"])
    assert probe["messages"][1:] == [{"role": "user", "content": COMPLETE}]
    assert all(value not in str(probe["messages"]) for value in (
        "旧候选查询", "123.456", "private-old-result", "private-old-pending", "待补参数"))
    assert service.conversations.pending_inputs(_event().scope_sha256("gq"), moment[0]) == []


@pytest.mark.parametrize("code", ["DEPENDENCY_UNAVAILABLE", "PERMISSION_DENIED"])
@pytest.mark.parametrize("budget_failure", [False, True])
def test_source_failure_preserves_a_previous_real_api_failure(tmp_path, code, budget_failure):
    from om_group_query_bot.api_client import QueryAPIError
    moment = [NOW]
    model = _ScriptedModel([
        _ask("权利金？"), _tool_response("evaluate_close", CLOSE_ARGS),
        _fresh_call(), RuntimeError("probe failed"),
    ])
    service = BotService(_settings(tmp_path, model_calls=2 if budget_failure else 4,
                                   user_per_minute=30), secret_provider=_Secrets(),
                         request=model, reply_fn=_Replies(), now=lambda: moment[0],
                         clock=lambda: CLOCK)
    service.agent._estimate = lambda _: 100
    api = service.agent.api_client = _FakeAPI(error=QueryAPIError(code, "injected"))
    _send(service, "om_a", "NVDA旧事实")
    moment[0] += timedelta(hours=23)
    result = _send(service, "om_b", COMPLETE)
    assert result["agent_status"] == "query_failed"
    assert len(api.calls) == 1
    with service.conversations.connect() as db:
        row = db.execute("SELECT body FROM outbox_payloads WHERE source_id='om_b'").fetchone()
        audit = db.execute("SELECT reason_codes FROM audit WHERE status='query_failed'").fetchone()
    assert ("权限不足" if code == "PERMISSION_DENIED" else "当前不可用") in row[0]
    assert "未执行实时查询" not in row[0]
    assert code in json.loads(audit[0])
