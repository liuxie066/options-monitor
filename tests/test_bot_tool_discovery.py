"""Discovery is model visibility, never an additional business execution authority."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from src.application.agent_tool_registry import AGENT_TOOL_REGISTRY
from src.application.bot.contracts import ExecutionContract
from src.application.bot.host import run_contract
from src.application.bot.scene import build_scene_manifest
from src.application.bot.tool_discovery import DISCOVERY_NAMES, MAX_MATCHES, ToolDiscovery, discovery_mode
from src.application.bot.tools import tool_descriptions
from tests.test_bot_python_runtime import MODEL, answer, call, script


def contract():
    return ExecutionContract("discovery-test", "discovery-test", "om_chat", "eval",
                             {"user_message": "解释策略"}, {"read_only": True}, {})


def discovery(names=("project_files", "runtime_status", "candidate_filter_explain")):
    return ToolDiscovery([{k: t[k] for k in ("name", "description", "input_schema")}
                          for t in tool_descriptions(names)], resident_tools=[])


@pytest.mark.parametrize("query,expected", [
    ("runtime_status", "runtime_status"),
    ("过滤 排除", "candidate_filter_explain"),
    ("源码 文件", "project_files"),
])
def test_local_search_hits_activate_next_request(query, expected):
    d = discovery()
    assert {t["name"] for t in d.descriptions()} == DISCOVERY_NAMES
    result = d.call("find_tools", {"query": query})
    assert result["ok"] and result["meta"]["read_only"]
    assert expected in result["data"]["activated"]
    assert len(result["data"]["matches"]) <= MAX_MATCHES
    assert all("parameters" in row and "input_schema" not in row for row in result["data"]["matches"])
    assert expected in {t["name"] for t in d.descriptions()}


@pytest.mark.parametrize("query,expected", [
    ("为什么 NVDA 被过滤", "candidate_filter_explain"),
    ("平仓建议", "close_advice_read"),
    ("最新每日简报", "daily_decision_brief_read"),
    ("源码 策略", "project_files"),
    ("收益 绩效", "option_performance_report"),
    ("运行健康状态", "runtime_status"),
    ("组合 资产 分布", "portfolio_query"),
    ("标的 中文名称", "symbol_resolve"),
])
def test_keywords_retrieve_expected_tool_from_all_29(query, expected):
    d = ToolDiscovery(build_scene_manifest(contract(), "recall").tool_descriptions, resident_tools=[])
    assert expected in d.call("find_tools", {"query": query})["data"]["activated"]


def test_two_misses_offer_exact_name_recovery_and_hit_resets_counter():
    d = discovery()
    first = d.call("find_tools", {"query": "zzzzquux"})
    assert first["ok"] and first["data"]["matches"] == []
    second = d.call("find_tools", {"query": "zzzzquux2"})
    assert not second["ok"] and second["error"]["code"] == "NEEDS_NARROWING"
    assert second["error"]["details"]["consecutive_misses"] == 2
    assert "get_tool_schema" in second["error"]["hint"]
    assert "project_files" in second["error"]["details"]["allowed_names"]
    assert d.call("get_tool_schema", {"name": "project_files"})["ok"]
    assert d.call("find_tools", {"query": "zzzzquux3"})["data"]["consecutive_misses"] == 1


@pytest.mark.parametrize("name", ["manage_symbols", "preview_notification", "unknown", "runtime_runs"])
def test_schema_cannot_expand_host_allowlist_or_execute_writes(monkeypatch, name):
    monkeypatch.setattr("src.application.bot.tools.execute_tool", lambda *a, **kw: pytest.fail("discovery executed business tool"))
    d = discovery()
    assert not d.call("get_tool_schema", {"name": name})["ok"]
    assert name not in {t["name"] for t in d.descriptions()}
    assert all(t.is_pure_read() and not t.side_effects for t in d.meta_tools)
    assert not DISCOVERY_NAMES & AGENT_TOOL_REGISTRY.keys()


def test_exact_schema_preserves_constraints_and_is_detached():
    d = discovery()
    result = d.call("get_tool_schema", {"name": "project_files"})
    expected = next(t for t in tool_descriptions(("project_files",)) if t["name"] == "project_files")
    assert result["data"]["tool"] == {k: expected[k] for k in ("name", "description", "input_schema")}
    result["data"]["tool"]["input_schema"].clear()
    assert next(t for t in d.descriptions() if t["name"] == "project_files")["input_schema"]


@pytest.mark.parametrize("args", [{}, {"query": ""}, {"query": 3}, {"query": "x", "name": "runtime_status"}])
def test_meta_tools_validate_arguments(args):
    assert discovery().call("find_tools", args)["ok"] is False


def test_modes_are_explicit_and_default_index():
    assert discovery_mode(None) == discovery_mode({}) == "index"
    assert discovery_mode({"OM_BOT_TOOL_DISCOVERY": "index"}) == "index"
    assert discovery_mode({"OM_BOT_TOOL_DISCOVERY": "full"}) == "full"
    with pytest.raises(ValueError):
        discovery_mode({"OM_BOT_TOOL_DISCOVERY": "typo"})


@pytest.mark.parametrize("environ", [None, {}])
def test_default_host_searches_before_business_execution(monkeypatch, environ):
    executed, requests = [], []
    monkeypatch.setattr("src.application.bot.tools.call_read_tool",
                        lambda name, payload, **kwargs: executed.append(name) or {"ok": True, "data": {}})
    result = run_contract(contract(), model_settings=MODEL, process_environ=environ,
        model_request=script([call("find_tools", {"query": "runtime_status"}, id="find"),
                              call("runtime_status", id="read"), answer()], requests))
    assert result.ok and executed == ["runtime_status"]
    assert {tool["name"] for tool in requests[0]["tools"]} == DISCOVERY_NAMES
    assert "runtime_status" in {tool["name"] for tool in requests[1]["tools"]}
    assert next(event.payload for event in result.events if event.type == "tool_discovery_mode")["mode"] == "index"


def test_full_and_index_share_same_29_business_tools_but_different_initial_visibility(monkeypatch):
    manifest = build_scene_manifest(contract(), "test")
    assert len(manifest.allowed_tools) == 29
    assert "position_advice_read" not in manifest.allowed_tools
    captured = {}
    monkeypatch.setattr("src.application.bot.tools.call_read_tool", lambda *a, **kw: pytest.fail("unexpected execution"))
    for mode in ("full", "index"):
        requests = []
        def request(**kwargs):
            requests.append(deepcopy(kwargs))
            return answer("需要更多信息。")
        # Allow clarification through a complete option word, with no evidence keyword.
        c = contract()
        c = ExecutionContract(c.contract_id, c.request_id, c.scene_name, c.execution_environment,
                              {"user_message": "腾讯put"}, c.policy, {})
        result = run_contract(c, model_settings=MODEL, process_environ={"OM_BOT_TOOL_DISCOVERY": mode}, model_request=request)
        assert result.ok
        captured[mode] = requests[0]
    assert {t["name"] for t in captured["full"]["tools"]} == set(manifest.allowed_tools)
    assert {t["name"] for t in captured["index"]["tools"]} == DISCOVERY_NAMES
    assert "candidate_filter_explain" in captured["index"]["messages"][0]["content"]


def test_unloaded_call_is_audited_recoverable_and_never_executes(monkeypatch):
    real = []
    monkeypatch.setattr("src.application.bot.tools.call_read_tool", lambda name, payload, **kw: real.append(name) or {"ok": True, "data": {"status": "ok"}})
    captured = []
    replies = [call("runtime_status", id="unloaded"), call("find_tools", {"query": "runtime_status"}, id="find"),
               call("runtime_status", id="loaded"), answer("已读取运行状态。")]
    result = run_contract(contract(), model_settings=MODEL, process_environ={"OM_BOT_TOOL_DISCOVERY": "index"},
                          model_request=script(replies, captured))
    assert result.ok and real == ["runtime_status"]
    error = next(json.loads(m["content"]) for m in captured[1]["messages"] if m.get("tool_call_id") == "unloaded")
    assert error["ok"] is False and "find_tools" in error["error"]["hint"]
    assert "runtime_status" not in {t["name"] for t in captured[1]["tools"]}
    assert "runtime_status" in {t["name"] for t in captured[2]["tools"]}
    calls = [e.payload["tool_name"] for e in result.events if e.type == "tool_call"]
    assert calls == ["runtime_status", "find_tools", "runtime_status"]
    results = [e.payload for e in result.events if e.type == "tool_result"]
    assert len(results) == 3
    assert not results[0]["ok"] and results[1]["ok"] and results[2]["ok"]
    assert "memory_source_ref" not in results[1]


def test_same_batch_search_does_not_allow_sibling_business_call(monkeypatch):
    real = []
    monkeypatch.setattr("src.application.bot.tools.call_read_tool", lambda name, payload, **kw: real.append(name) or {"ok": True, "data": {}})
    search = call("find_tools", {"query": "runtime_status"}, id="find")
    search["message"]["tool_calls"].extend(call("runtime_status", id="tooearly")["message"]["tool_calls"])
    captured = []
    result = run_contract(contract(), model_settings=MODEL, process_environ={"OM_BOT_TOOL_DISCOVERY": "index"},
                          model_request=script([search, call("runtime_status", id="later"), answer()], captured))
    assert result.ok and real == ["runtime_status"]
    early = next(json.loads(m["content"]) for m in captured[1]["messages"] if m.get("tool_call_id") == "tooearly")
    assert not early["ok"] and early["error"]["code"] == "INPUT_ERROR"


def test_discovery_activation_is_not_shared_across_runs():
    one, two = discovery(), discovery()
    one.call("find_tools", {"query": "runtime_status"})
    assert "runtime_status" in {t["name"] for t in one.descriptions()}
    assert "runtime_status" not in {t["name"] for t in two.descriptions()}


def test_exact_search_does_not_activate_unrelated_tools_and_hits_are_bounded():
    d = ToolDiscovery(build_scene_manifest(contract(), "catalog").tool_descriptions, resident_tools=[])
    assert d.call("find_tools", {"query": "runtime_status"})["data"]["activated"] == ["runtime_status"]
    assert len(d.call("find_tools", {"query": "account status portfolio runtime"})["data"]["matches"]) == MAX_MATCHES
    assert not d.call("find_tools", {"query": "   "})["data"]["matches"]
    with pytest.raises(ValueError, match="duplicate"):
        ToolDiscovery(tool_descriptions(("runtime_status",)) * 2, resident_tools=[])


def test_host_two_misses_then_exact_schema_recovers_and_persists_audit(tmp_path, monkeypatch):
    from src.application.bot.host_store import BotHostStore
    store = BotHostStore(tmp_path / "audit.sqlite3")
    executed = []
    monkeypatch.setattr("src.application.bot.tools.call_read_tool",
                        lambda name, payload, **kw: executed.append(name) or {"ok": True, "data": {"status": "synthetic"}})
    captured = []
    replies = [call("find_tools", {"query": "zzzznonesuchunmatchable"}, id="miss1"),
               call("find_tools", {"query": "zzzznonesuchunmatchable"}, id="miss2"),
               call("get_tool_schema", {"name": "runtime_status"}, id="schema"),
               call("runtime_status", id="read"), answer("已读取状态。")]
    result = run_contract(contract(), model_settings=MODEL, host_store=store,
        process_environ={"OM_BOT_TOOL_DISCOVERY": "index"}, model_request=script(replies, captured))
    assert result.ok and executed == ["runtime_status"]
    observations = {m["tool_call_id"]: json.loads(m["content"]) for m in captured[-1]["messages"] if m.get("role") == "tool"}
    assert observations["miss2"]["error"]["code"] == "NEEDS_NARROWING"
    assert observations["miss2"]["error"]["details"]["retryable"]
    events = json.loads(store.run_record(result.run_id)["events_json"])
    calls = [e["payload"]["tool_name"] for e in events if e["type"] == "tool_call"]
    assert calls == ["find_tools", "find_tools", "get_tool_schema", "runtime_status"]
    results = [e["payload"] for e in events if e["type"] == "tool_result"]
    assert len(results) == len(calls)
    assert all("memory_source_ref" not in e for e in results[:-1])
    assert "runtime_status" not in {t["name"] for t in captured[2]["tools"]}
    assert "runtime_status" in {t["name"] for t in captured[3]["tools"]}


def test_index_completes_in_small_context_while_full_preserves_capacity_guard(monkeypatch):
    monkeypatch.setattr("src.application.bot.tools.call_read_tool", lambda *a, **kw: {"ok": True, "data": {}})
    settings = replace(MODEL, context_window_tokens=24_000)
    captured = []
    result = run_contract(contract(), model_settings=settings, process_environ={"OM_BOT_TOOL_DISCOVERY": "index"},
        model_request=script([call("find_tools", {"query": "runtime_status"}, id="find"), call("runtime_status", id="read"), answer()], captured))
    assert result.ok and len(captured) == 3, result
    full = []
    result = run_contract(contract(), model_settings=settings, process_environ={"OM_BOT_TOOL_DISCOVERY": "full"},
                          model_request=script([answer()], full))
    assert not result.ok and result.error["reason"] == "context_capacity"
    assert not full


@pytest.mark.parametrize("mode", [None, "index", "full"])
def test_harness_passes_discovery_mode_and_invalid_setting_is_explicit(monkeypatch, mode):
    from src.application.bot.local_harness import run_prepared_contract
    if mode is None:
        monkeypatch.delenv("OM_BOT_TOOL_DISCOVERY", raising=False)
    else:
        monkeypatch.setenv("OM_BOT_TOOL_DISCOVERY", mode)
    first_call = ({"name": "project_context", "arguments": {}} if mode == "full" else
                  {"name": "find_tools", "arguments": {"query": "runtime_status"}})
    result = run_prepared_contract(contract(), model_turn_json=json.dumps([
        {"tool_calls": [first_call]},
        {"text": "已完成读取。"},
    ]))
    assert result.ok, result
    assert next(e.payload for e in result.events if e.type == "tool_discovery_mode")["mode"] == (mode or "index")


@pytest.mark.parametrize("mode", ["", "bad"])
def test_harness_rejects_explicit_invalid_discovery_mode(monkeypatch, mode):
    from src.application.bot.local_harness import run_prepared_contract

    monkeypatch.setenv("OM_BOT_TOOL_DISCOVERY", mode)
    result = run_prepared_contract(contract(), model_turn_json=json.dumps([{"text": "不应调用模型。"}]))
    assert not result.ok and result.error["code"] == "CONFIG_ERROR"


@pytest.mark.parametrize("mode", ["full", "index"])
def test_host_memory_and_control_preview_remain_resident(monkeypatch, tmp_path, mode):
    from src.application.bot.control_handoff import CONTROL_PREVIEW_TOOL
    from src.application.bot.host_store import BotHostStore
    from src.application.bot.session import session_key_for_contract
    from tests.test_bot_cross_market_read import _contract, _scope
    _configs, fixed = _scope(tmp_path, monkeypatch, ["us"])
    c = _contract(fixed)
    captured = []
    result = run_contract(c, model_settings=MODEL, host_store=BotHostStore(tmp_path / "resident.sqlite3"),
        session_key=session_key_for_contract(c), process_environ={"OM_BOT_TOOL_DISCOVERY": mode},
        control_preview_specs=({"intent_name": "symbol_edit", "arguments": ["symbol", "set"]},),
        model_request=script([call("bot_memory", {"action": "list"}), answer("尚无偏好记录。")], captured))
    assert result.ok, result
    for request in captured:
        assert {"bot_memory", CONTROL_PREVIEW_TOOL} <= {t["name"] for t in request["tools"]}
    event = next(e for e in result.events if e.type == "tool_result")
    assert event.payload["ok"] and "memory_source_ref" not in event.payload


@pytest.mark.parametrize("raw", ["{", "[]"])
def test_invalid_json_arguments_have_standard_envelope_and_durable_audit(tmp_path, raw):
    from src.application.bot.host_store import BotHostStore
    store = BotHostStore(tmp_path / "invalid.sqlite3")
    turn = call("find_tools")
    turn["message"]["tool_calls"][0]["function"]["arguments"] = raw
    captured = []
    result = run_contract(contract(), model_settings=MODEL, host_store=store,
        process_environ={"OM_BOT_TOOL_DISCOVERY": "index"}, model_request=script([turn, answer()], captured))
    assert result.ok, result
    observation = json.loads(captured[-1]["messages"][-1]["content"])
    assert observation["schema_version"] == "1.0" and observation["tool_name"] == "find_tools"
    assert observation["error"]["code"] == "INPUT_ERROR"
    events = json.loads(store.run_record(result.run_id)["events_json"])
    assert sum(e["type"] == "tool_call" for e in events) == sum(e["type"] == "tool_result" for e in events) == 1


def test_tool_budget_rejection_keeps_durable_audit(tmp_path):
    from src.application.bot.host_store import BotHostStore
    store = BotHostStore(tmp_path / "budget.sqlite3")
    names = build_scene_manifest(contract(), "tools").allowed_tools[:13]
    turn = call("get_tool_schema", {"name": names[0]}, id="0")
    for index, name in enumerate(names[1:], 1):
        turn["message"]["tool_calls"].extend(call("get_tool_schema", {"name": name}, id=str(index))["message"]["tool_calls"])
    captured = []
    result = run_contract(contract(), model_settings=replace(MODEL, context_window_tokens=200_000), host_store=store,
        process_environ={"OM_BOT_TOOL_DISCOVERY": "index"}, model_request=script([turn, answer()], captured))
    assert result.ok, result
    outputs = [json.loads(m["content"]) for m in captured[-1]["messages"] if m["role"] == "tool"]
    assert len(outputs) == 13 and outputs[-1]["error"]["code"] == "BUDGET_EXHAUSTED"
    assert all(value["schema_version"] == "1.0" for value in outputs)
    events = json.loads(store.run_record(result.run_id)["events_json"])
    assert sum(e["type"] == "tool_call" for e in events) == sum(e["type"] == "tool_result" for e in events) == 13


@pytest.mark.parametrize("name", ["receipt_read", "daily_decision_brief_read"])
def test_host_preserves_schema_metadata_in_get_schema(name):
    captured = []
    result = run_contract(contract(), model_settings=MODEL, process_environ={"OM_BOT_TOOL_DISCOVERY": "index"},
        model_request=script([call("get_tool_schema", {"name": name}), answer("工具定义已加载。")], captured))
    assert result.ok, result
    observation = json.loads(captured[-1]["messages"][-1]["content"])
    native = next(t for t in captured[-1]["tools"] if t["name"] == name)
    assert observation["data"]["tool"] == native


@pytest.mark.parametrize("api", ["openai-completions", "openai-responses"])
def test_dynamic_schemas_reach_both_real_provider_adapters(monkeypatch, api):
    from src.infrastructure import openai_chat_completions, openai_responses
    wire = []
    def post(_url, payload, **kwargs):
        wire.append(deepcopy(payload))
        if len(wire) == 1:
            function = {"name": "find_tools", "arguments": json.dumps({"query": "runtime_status"})}
        elif len(wire) == 2:
            function = {"name": "runtime_status", "arguments": "{}"}
        else:
            function = None
        if api == "openai-completions":
            message = {"role": "assistant", "content": "状态已读取。"}
            if function:
                message["tool_calls"] = [{"id": str(len(wire)), "type": "function", "function": function}]
            return {"choices": [{"message": message, "finish_reason": "tool_calls" if function else "stop"}]}
        output = ([{"type": "function_call", "call_id": str(len(wire)), **function}] if function
                  else [{"type": "message", "content": [{"type": "output_text", "text": "状态已读取。"}]}])
        return {"status": "completed", "output": output}
    monkeypatch.setattr(openai_chat_completions, "_post_json", post)
    monkeypatch.setattr(openai_responses, "_post_json", post)
    reads = []
    monkeypatch.setattr("src.application.bot.tools.call_read_tool",
                        lambda name, payload, **kw: reads.append(name) or {"ok": True, "data": {}})
    result = run_contract(contract(), model_settings=replace(MODEL, api_kind=api),
                          process_environ={"OM_BOT_TOOL_DISCOVERY": "index", "OM_BOT_MODEL_API_KEY": "synthetic"})
    assert result.ok and reads == ["runtime_status"] and len(wire) == 3, result
    initial = [t.get("function", t) for t in wire[0]["tools"]]
    activated = [t.get("function", t) for t in wire[1]["tools"]]
    assert {t["name"] for t in initial} == DISCOVERY_NAMES
    business = next(t for t in activated if t["name"] == "runtime_status")
    expected = next(t for t in build_scene_manifest(contract(), "wire").tool_descriptions if t["name"] == "runtime_status")
    assert business["parameters"] == expected["input_schema"]


def test_repetition_budget_stops_later_handlers_in_the_same_batch():
    turn = call("find_tools", {"query": "runtime_status"}, id="find1")
    for ident in ("find2", "find3"):
        turn["message"]["tool_calls"].extend(call("find_tools", {"query": "runtime_status"}, id=ident)["message"]["tool_calls"])
    turn["message"]["tool_calls"].extend(call("get_tool_schema", {"name": "project_files"}, id="after-limit")["message"]["tool_calls"])
    requests = []
    result = run_contract(contract(), model_settings=MODEL, model_request=script([turn, answer()], requests))
    assert result.ok, result
    observations = {m["tool_call_id"]: json.loads(m["content"]) for m in requests[-1]["messages"] if m.get("role") == "tool"}
    assert observations["find3"]["error"]["code"] == "BUDGET_EXHAUSTED"
    assert observations["after-limit"]["error"]["code"] == "BUDGET_EXHAUSTED"
    assert not requests[-1]["tools"]


@pytest.mark.parametrize("provider_failures", [1, 2])
def test_provider_failure_after_discovery_retries_before_finalizing(monkeypatch, provider_failures):
    requests, reads = [], []
    def request(**kwargs):
        requests.append(deepcopy(kwargs))
        if len(requests) == 1:
            return call("find_tools", {"query": "runtime_status"}, id="find")
        if len(requests) <= provider_failures + 1:
            raise OSError("synthetic transport failure")
        if not reads and "runtime_status" in {tool["name"] for tool in kwargs["tools"]}:
            return call("runtime_status", id="read")
        return answer("已读取状态。")
    monkeypatch.setattr("src.application.bot.tools.call_read_tool",
                        lambda name, payload, **kwargs: reads.append(name) or {"ok": True, "data": {}})
    result = run_contract(contract(), model_settings=replace(MODEL, max_attempts=2), model_request=request)
    if provider_failures == 1:
        assert result.ok and reads == ["runtime_status"], result
        assert len(requests) == 4
    else:
        assert not result.ok and result.error["code"] == "MODEL_ERROR"
        assert len(requests) == 3 and reads == []
    assert "runtime_status" in {tool["name"] for tool in requests[2]["tools"]}


def test_search_activation_cannot_outlive_read_scope_revocation(monkeypatch, tmp_path):
    from src.application.bot.host_store import BotHostStore
    from src.application.bot.session import session_key_for_contract
    from tests.test_bot_cross_market_read import _contract, _scope
    _configs, fixed = _scope(tmp_path, monkeypatch, ["us"])
    c = _contract(fixed)
    executed = []
    monkeypatch.setattr("src.application.bot.tools.call_read_tool", lambda *a, **kw: executed.append(a))
    requests = []
    def request(**kwargs):
        requests.append(deepcopy(kwargs))
        if len(requests) == 1:
            return call("find_tools", {"query": "runtime_status"}, id="find")
        (tmp_path / "config.bot.json").write_text(json.dumps({"bot": {"enabled": True, "read_markets": ["us", "hk"]}}))
        return call("runtime_status", id="read")
    store = BotHostStore(tmp_path / "revoked.sqlite3")
    session_key = session_key_for_contract(c)
    result = run_contract(c, model_settings=MODEL, host_store=store, session_key=session_key,
        process_environ={"OM_BOT_TOOL_DISCOVERY": "index"}, model_request=request)
    assert result.error["code"] == "SCOPE_REVOKED" and not executed
    assert "runtime_status" in {t["name"] for t in requests[1]["tools"]}
    assert store.chat_messages(session_key) == []
