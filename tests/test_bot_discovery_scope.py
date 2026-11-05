"""Expanded Scene selectors stay bound before any external portfolio/report read."""
from dataclasses import replace
from pathlib import Path

import pytest

from src.application.bot import tools
from src.application.bot.host import run_contract
from tests.test_bot_cross_market_read import _contract, _scope
from tests.test_bot_python_runtime import MODEL, answer, call, script


@pytest.mark.parametrize("name,args,market", [
    ("daily_decision_brief_read", {}, "us"),
    ("close_advice_read", {"query": {}}, "hk"),
    ("decision_history_read", {}, "hk"),
])
def test_report_native_market_is_bound(monkeypatch, tmp_path, name, args, market):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    account = configs[market][1]["accounts"][0]
    args = {**args, **({"config_key": market} if name == "close_advice_read" else {"market": market.upper()})}
    if name == "close_advice_read":
        args["query"] = {"account": account}
    else:
        args["account"] = account
    payload, error = tools.build_tool_payload(name, args, fixed_input=fixed)
    assert error is None
    if name == "close_advice_read":
        assert payload["config_path"] == str(configs[market][0].resolve())
        assert payload["market_scope"] == market
    else:
        assert payload["market"] == market.upper()


@pytest.mark.parametrize("mode", ["full", "index"])
@pytest.mark.parametrize("name,args", [
    ("daily_decision_brief_read", {}),
    ("close_advice_read", {}),
    ("close_advice_read", {"market_scope": "all", "query": {"account": "lx"}}),
    ("close_advice_read", {"market_scope": "hk", "query": {"account": "lx"}}),
    ("portfolio_query", {"view": "accounts"}),
    ("portfolio_query", {"view": "overview"}),
    ("portfolio_query", {"view": "distribution", "accounts": ["unconfigured"]}),
    ("portfolio_pnl_bridge", {"period": "mtd", "accounts": ["unconfigured"]}),
    ("portfolio_assignment_scenario", {"accounts": ["unconfigured"]}),
    ("portfolio_query", {"config_key": "hk", "view": "cash", "account": "lx"}),
])
def test_discovery_never_bypasses_selector_rejections(monkeypatch, tmp_path, mode, name, args):
    _configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    attempted = []
    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("rejected request reached business execution")
    monkeypatch.setattr(tools, "execute_tool", forbidden)
    replies = ([call("find_tools", {"query": name}, id="find")] if mode == "index" else [])
    replies += [call(name, args, id="business"), answer("该范围尚未获准读取。")]
    captured = []
    c = replace(_contract(fixed), execution_environment="eval")
    result = run_contract(c, model_settings=MODEL, process_environ={"OM_BOT_TOOL_DISCOVERY": mode},
                          model_request=script(replies, captured))
    assert result.ok, result
    observations = [event.payload for event in result.events if event.type == "tool_result" and event.payload["tool_name"] == name]
    assert len(observations) == 1 and not observations[0]["ok"]
    assert not attempted


@pytest.mark.parametrize("mode", ["full", "index"])
def test_real_portfolio_owner_receives_only_selected_accounts(monkeypatch, tmp_path, mode):
    from src.application.agent_tools import portfolio

    configs, fixed = _scope(tmp_path, monkeypatch, ["us"])
    account = configs["us"][1]["accounts"][0]
    client_calls, config_reads = [], []
    class Client:
        def read_view(self, view, *, query, timeout):
            client_calls.append((view, query))
            return {"success": True, "accounts": [account], "total_value": 123}
    def resolve(config, **kwargs):
        config_reads.append(config)
        return Client()
    monkeypatch.setattr(portfolio, "resolve_portfolio_management_client", resolve)
    replies = ([call("find_tools", {"query": "portfolio_query"}, id="find")] if mode == "index" else [])
    replies += [call("portfolio_query", {"view": "overview", "accounts": [account]}, id="read"), answer("已读取指定账户的组合记录。")]
    result = run_contract(replace(_contract(fixed), execution_environment="eval"), model_settings=MODEL,
                          process_environ={"OM_BOT_TOOL_DISCOVERY": mode}, model_request=script(replies, []))
    assert result.ok, result
    observation = next(event.payload for event in result.events if event.type == "tool_result" and event.payload["tool_name"] == "portfolio_query")
    assert observation["ok"], observation
    assert client_calls == [("overview", {"accounts": account})]
    assert config_reads[0]["accounts"] == configs["us"][1]["accounts"]
    assert observation["tool_input"]["accounts"] == [account]


def test_portfolio_cannot_ignore_a_trusted_config_path(monkeypatch, tmp_path):
    configs, _fixed = _scope(tmp_path, monkeypatch, ["us"])
    account = configs["us"][1]["accounts"][0]
    payload, error = tools.build_tool_payload("portfolio_query", {"view": "cash", "account": account},
        fixed_input={"config_key": "us", "config_path": str(Path(tmp_path) / "different-us.json")})
    assert payload is None and "configuration path" in error


def test_plural_accounts_are_normalized_and_checked_for_every_portfolio_reader(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us"])
    account = configs["us"][1]["accounts"][0]
    for name, args in [("portfolio_query", {"view": "distribution"}),
                       ("portfolio_pnl_bridge", {"period": "mtd"}),
                       ("portfolio_assignment_scenario", {})]:
        payload, error = tools.build_tool_payload(name, {**args, "accounts": [account.upper()]}, fixed_input=fixed)
        assert error is None and payload["accounts"] == [account]
        payload, error = tools.build_tool_payload(name, {**args, "accounts": [account, "unconfigured"]}, fixed_input=fixed)
        assert payload is None and "outside" in error
