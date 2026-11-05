"""Scripted comparison of actual Host discovery; no claim about real-model recall."""
from copy import deepcopy
import json

import pytest

from src.application.bot.contracts import ExecutionContract
from src.application.bot.eval_fixtures import fixture_observations
from src.application.bot.host import run_contract
from src.application.bot.runtime import token_estimate
from tests.test_bot_python_runtime import MODEL, answer, call, script


CASES = [
    ("opening_candidate_snapshot_diagnostics_model_ready", "runtime_status candidate_filter_explain", [
        ("runtime_status", {}),
        ("candidate_filter_explain", {"symbol": "NVDA", "account": "lx", "run_id": "synthetic"}),
    ], "记录显示 NVDA 因到期前财报事件被过滤。"),
    ("close_advice_notification_diagnostics_model_ready", "runtime_status close_advice_read", [
        ("runtime_status", {}), ("close_advice_read", {"query": {"account": "lx"}}),
    ], "本次扫描没有生成通知内容；已有平仓建议为空。"),
]


def compare_modes(fixture_id, query, reads, text, config_path):
    fixtures = {item["tool_name"]: item for item in fixture_observations(fixture_id)}
    contract = ExecutionContract("discovery-eval", "discovery-eval", "om_chat", "eval",
                                 {"user_message": "解释已有记录", "fixture_id": fixture_id,
                                  "config_key": "us", "config_path": str(config_path)}, {"read_only": True}, {})
    results, observations, metrics = {}, {}, {}
    for mode in ("full", "index"):
        requests = []
        replies = [call(name, args, id=f"read-{index}") for index, (name, args) in enumerate(reads)]
        if mode == "index":
            replies.insert(0, call("find_tools", {"query": query}, id="search"))
        replies.append(answer(text))
        def request(**kwargs):
            # Host appends to its history after each request; freeze measured inputs.
            requests.append(deepcopy(kwargs))
            return replies.pop(0)
        result = run_contract(contract, model_settings=MODEL, process_environ={"OM_BOT_TOOL_DISCOVERY": mode},
                              model_request=request)
        assert result.ok, result
        results[mode] = result.user_response
        observations[mode] = [event.payload for event in result.events if event.type == "tool_result"
                              and event.payload["tool_name"] in fixtures]
        assert [row["tool_name"] for row in observations[mode]] == [name for name, _ in reads]
        assert all(row["ok"] for row in observations[mode]), observations[mode]
        for row in observations[mode]:
            assert row["data"] == fixtures[row["tool_name"]]["data"]
        metrics[mode] = {"model_requests": len(requests),
                         "estimated_input_tokens": sum(token_estimate([r["messages"], r["tools"]]) for r in requests)}
    assert results["index"] == results["full"] == text
    assert [row["data"] for row in observations["index"]] == [row["data"] for row in observations["full"]]
    return metrics


@pytest.mark.parametrize("fixture_id,query,reads,text", CASES)
def test_scripted_discovery_preserves_fixture_results_and_reduces_input(monkeypatch, example_config_path, fixture_id, query, reads, text):
    fixtures = {item["tool_name"]: item for item in fixture_observations(fixture_id)}
    monkeypatch.setattr("src.application.bot.tools.call_read_tool", lambda name, payload, **kwargs: deepcopy(fixtures[name]))
    metrics = compare_modes(fixture_id, query, reads, text, example_config_path)
    assert metrics["full"]["model_requests"] == 3
    assert metrics["index"]["model_requests"] == 4
    assert metrics["index"]["estimated_input_tokens"] < metrics["full"]["estimated_input_tokens"]


def test_discovery_misses_do_not_become_a_business_result(monkeypatch):
    attempted = []
    monkeypatch.setattr("src.application.bot.tools.call_read_tool", lambda *args, **kwargs: attempted.append(args))
    requests = []
    contract = ExecutionContract("miss-eval", "miss-eval", "om_chat", "eval",
                                 {"user_message": "检索尚未支持的功能"}, {"read_only": True}, {})
    result = run_contract(contract, model_settings=MODEL, process_environ={"OM_BOT_TOOL_DISCOVERY": "index"},
        model_request=script([call("find_tools", {"query": "zzzznonesuchunmatchable"}, id="first"),
                              call("find_tools", {"query": "zzzznonesuchunmatchable"}, id="second"),
                              answer("两次检索未找到合适工具，尚未读取业务数据。")], requests))
    assert result.ok and not attempted
    assert json.loads(requests[-1]["messages"][-1]["content"])["error"]["code"] == "NEEDS_NARROWING"
