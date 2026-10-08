"""Trusted Bot market routing across generated runtime configs; no broker access."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict

import pytest

from src.application.bot import tools
from src.application.bot.contracts import BotRequest, BotScope, ExecutionContract, new_id
from src.application.bot.host import run_contract
from src.application.bot.host_store import BotHostStore
from src.application.bot.scene import build_scene_manifest
from src.application.bot.memory_worker import configured_memory_scope, verified_sources_from_run
from src.application.bot.model_config import load_bot_read_scope
from src.application.bot.service import prepare_contract
from src.application.bot.session import session_key_for_contract
from src.application.config_yaml import build_yaml_runtime_config_file
from src.application.agent_tool_contracts import AgentToolError
from tests.bot_pi_test_support import _TEST_MODEL


def _scope(tmp_path, monkeypatch, markets):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    configs = {}
    for market in ("us", "hk"):
        path = tmp_path / f"config.{market}.json"
        build_yaml_runtime_config_file(
            repo_root=root,
            market=market,
            config_path=root / "configs/examples/config.yaml.example",
            output_config_path=path,
        )
        configs[market] = (path, json.loads(path.read_text()))
    assistant = tmp_path / "config.bot.json"
    assistant.write_text(json.dumps({"bot": {'enabled': True, 'read_markets': markets}}))
    allowed, generation = load_bot_read_scope(config_path=assistant, primary_market="us")
    return configs, {
        "config_key": "us", "config_path": str(configs["us"][0]),
        "read_markets": sorted(allowed), "read_generation": generation,
        "bot_config_path": str(assistant),
    }


def test_dual_market_symbol_routes_to_hk_and_keeps_hk_account(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    account = configs["hk"][1]["accounts"][0]
    payload, error = tools.build_tool_payload(
        "trade_attribution_read", {"symbol": "3690.HK", "account": account}, fixed_input=fixed)
    assert error is None
    assert payload["config_key"] == "hk"
    assert payload["config_path"] == str(configs["hk"][0].resolve())
    assert payload["account"] == account


def test_ungranted_or_conflicting_hk_read_never_builds_a_payload(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us"])
    account = configs["hk"][1]["accounts"][0]
    payload, error = tools.build_tool_payload(
        "trade_attribution_read", {"symbol": "3690.HK", "account": account}, fixed_input=fixed)
    assert payload is None and "outside" in error

    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    payload, error = tools.build_tool_payload(
        "trade_attribution_read", {"config_key": "us", "symbol": "3690.HK", "account": account}, fixed_input=fixed)
    assert payload is None and "conflict" in error


@pytest.mark.parametrize("markets", [[], ["US"], ["us", "us"], ["cn"]])
def test_invalid_bot_read_grant_fails_closed(tmp_path, markets):
    assistant = tmp_path / "assistant.json"
    assistant.write_text(json.dumps({"bot": {'enabled': False, 'read_markets': markets}}))
    with pytest.raises(AgentToolError, match="Bot config validation failed"):
        load_bot_read_scope(config_path=assistant, primary_market="us")
    assistant.write_text(json.dumps({"bot": {'enabled': False, 'read_markets': ['hk']}}))
    with pytest.raises(ValueError, match="include the channel market"):
        load_bot_read_scope(config_path=assistant, primary_market="us")


def test_dual_market_ambiguous_read_requires_market_and_revocation_stops_it(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    account = configs["hk"][1]["accounts"][0]
    payload, error = tools.build_tool_payload(
        "trade_attribution_read", {"account": account}, fixed_input=fixed)
    assert payload is None and "specify the market" in error
    assistant = tmp_path / "config.bot.json"
    assistant.write_text(json.dumps({"bot": {'enabled': True, 'read_markets': ['us']}}))
    payload, error = tools.build_tool_payload(
        "trade_attribution_read", {"config_key": "hk", "account": account}, fixed_input=fixed)
    assert payload is None and "changed" in error


def test_assignment_events_are_the_only_bot_position_action(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    payload, error = tools.build_tool_payload(
        "option_positions_read", {"config_key": "hk", "action": "list"}, fixed_input=fixed)
    assert payload is None and error is not None
    payload, error = tools.build_tool_payload(
        "option_positions_read", {"config_key": "hk", "action": "events", "symbol": "3690.HK"}, fixed_input=fixed)
    account = configs["hk"][1]["accounts"][0]
    assert error is None and payload["account"] == account
    payload, error = tools.build_tool_payload(
        "option_positions_read", {"config_key": "hk", "action": "events", "symbol": "3690.HK", "account": account}, fixed_input=fixed)
    assert error is None and payload["config_key"] == "hk" and payload["action"] == "events"


def _contract(fixed):
    request = BotRequest(
        request_id=new_id("cross_market"), source_entry="test", user_message="查 3690.HK 的本地事件",
        explicit_scope=BotScope(config_key="us", config_path=fixed["config_path"]),
        execution_environment="channel",
        trusted_tool_scope={
            **fixed, "authenticated_channel": "feishu", "authenticated_sender_id": "sender",
            "authenticated_conversation_id": "conversation", "authority_scope": "key:us",
        },
    )
    return prepare_contract(request, reference_year=2026)


def test_nested_account_is_checked_against_the_selected_config(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    account = configs["hk"][1]["accounts"][0]
    payload, error = tools.build_tool_payload(
        "option_positions_read", {"action": "list", "query": {"account": account}},
        fixed_input={"config_key": "us", "config_path": fixed["config_path"]})
    assert error is None and payload["query"]["account"] == account
    payload, error = tools.build_tool_payload(
        "option_positions_read", {"action": "list", "query": {"account": "not_configured"}},
        fixed_input={"config_key": "us", "config_path": fixed["config_path"]})
    assert payload is None and "query.account" in error


def test_dual_market_memory_uses_market_qualified_account_and_config_generation(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    account = configs["hk"][1]["accounts"][0]
    contract_a = _contract(fixed)
    scope_a = configured_memory_scope(contract_a)
    session_a = session_key_for_contract(contract_a)
    assert f"hk:{account}" in scope_a.allowed_accounts
    with monkeypatch.context() as isolated_env:
        isolated_env.setenv("OM_RUNTIME_ROOT", str(tmp_path / "other-runtime"))
        with pytest.raises(ValueError, match="MEMORY_ACCOUNT_CONFIG_INVALID"):
            configured_memory_scope(contract_a)
    observation = {
        "type": "tool_result", "timestamp": "2026-09-29T00:00:00Z", "payload": {
            "ok": True, "tool_name": "trade_attribution_read", "ref": "obv_hk",
            "host_scope": {"market": "hk", "account": account},
            "tool_input": {"account": account, "config_key": "hk"},
            "data": {"account": account, "market": "hk", "rows": [], "coverage": {"status": "complete"}},
        },
    }
    run = {"run_id": "r1", "contract_json": json.dumps(asdict(contract_a)),
           "events_json": json.dumps([observation])}
    sources = verified_sources_from_run(run, scope=scope_a)
    assert sources["evidence:r1:obv_hk"]["account_scope"] == f"hk:{account}"
    observation["payload"]["host_scope"]["market"] = "us"
    assert verified_sources_from_run(run | {"events_json": json.dumps([observation])}, scope=scope_a)[
        "evidence:r1:obv_hk"]["account_scope"] == f"us:{account}"

    assistant = tmp_path / "config.bot.json"
    original = assistant.read_bytes()
    stat = assistant.stat()
    assistant.write_text(json.dumps({"bot": {'enabled': True, 'read_markets': ['us']}}))
    allowed, generation_b = load_bot_read_scope(config_path=assistant, primary_market="us")
    contract_b = _contract({**fixed, "read_markets": sorted(allowed), "read_generation": generation_b})
    scope_b = configured_memory_scope(contract_b)
    session_b = session_key_for_contract(contract_b)
    assistant.write_bytes(original)
    os.utime(assistant, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    allowed, generation_a2 = load_bot_read_scope(config_path=assistant, primary_market="us")
    contract_a2 = _contract({**fixed, "read_markets": sorted(allowed), "read_generation": generation_a2})
    scope_a2 = configured_memory_scope(contract_a2)
    session_a2 = session_key_for_contract(contract_a2)
    assert len({scope_a.owner_scope, scope_b.owner_scope, scope_a2.owner_scope}) == 3
    assert len({session_a, session_b, session_a2}) == 3


def test_host_rejects_in_flight_revocation_before_tool_read(monkeypatch, tmp_path):
    _configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    contract = _contract(fixed)
    assistant = tmp_path / "config.bot.json"

    def model_request(**_kwargs):
        assistant.write_text(json.dumps({"bot": {'enabled': True, 'read_markets': ['us']}}))
        return {"message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": "read1", "type": "function", "function": {
                "name": "trade_attribution_read", "arguments": json.dumps({
                    "config_key": "hk", "account": "lx", "symbol": "3690.HK"})}}]},
            "finish_reason": "tool_calls"}

    result = run_contract(contract, model_settings=_TEST_MODEL, model_request=model_request,
                          session_key=session_key_for_contract(contract))
    assert result.status == "failed" and result.error["code"] == "SCOPE_REVOKED"
    assert not any(event.type == "tool_result" for event in result.events)


def test_rebuild_with_same_grant_isolates_channel_session_and_memory(monkeypatch, tmp_path):
    _configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    first = _contract(fixed)
    first_session = session_key_for_contract(first)
    first_owner = configured_memory_scope(first).owner_scope
    assistant = tmp_path / "config.bot.json"
    stat = assistant.stat()
    os.utime(assistant, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    _markets, generation = load_bot_read_scope(config_path=assistant, primary_market="us")
    second = _contract({**fixed, "read_generation": generation})
    assert first_session != session_key_for_contract(second)
    assert first_owner != configured_memory_scope(second).owner_scope


def test_host_bot_config_unavailable_before_start_is_not_ready(monkeypatch, tmp_path):
    _configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    contract = _contract(fixed)
    (tmp_path / "config.bot.json").unlink()
    store = BotHostStore(tmp_path / "host.sqlite3")
    result = run_contract(contract, model_settings=_TEST_MODEL, host_store=store, session_key="old-session")
    assert result.status == "not_ready" and result.error["code"] == "SCENE_PREPARATION_FAILED"
    assert not store.path.exists()


def test_host_revocation_after_tool_read_blocks_answer_and_outbox(monkeypatch, tmp_path):
    _configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    contract = _contract(fixed)
    assistant = tmp_path / "config.bot.json"
    monkeypatch.setattr(tools, "call_read_tool", lambda *_args, **_kwargs: {"ok": True, "data": {"rows": [], "market": "hk", "account": "lx"}})
    turns = iter((
        {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "type": "function", "function": {
            "name": "trade_attribution_read", "arguments": json.dumps({"config_key": "hk", "account": "lx", "symbol": "3690.HK"})}}]},
         "finish_reason": "tool_calls"},
        {"message": {"role": "assistant", "content": "本地归属记录为空。"}, "finish_reason": "stop"},
    ))
    def model_request(**_kwargs):
        turn = next(turns)
        if turn["finish_reason"] == "stop":
            assistant.write_text(json.dumps({"bot": {'enabled': True, 'read_markets': ['us']}}))
        return turn
    store = BotHostStore(tmp_path / "host.sqlite3")
    result = run_contract(contract, model_settings=_TEST_MODEL, model_request=model_request,
                          host_store=store, session_key=session_key_for_contract(contract))
    assert result.status == "failed" and result.error["code"] == "SCOPE_REVOKED"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_reply_outbox").fetchone()[0] == 0


def test_scene_only_exposes_event_fields(monkeypatch, tmp_path):
    _configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    description = next(item for item in build_scene_manifest(_contract(fixed), "scene").tool_descriptions
                       if item["name"] == "option_positions_read")
    fields = set(description["input_schema"]["properties"])
    assert fields == {"config_key", "action", "broker", "account", "limit", "cursor", "include_total",
                      "position_effect", "symbol", "option_type", "strike", "exp"}
    assert description["default_input"]["action"] == "events"
    assert all(example["input"]["action"] == "events" for example in description["examples"])


def test_receipt_conflict_does_not_suggest_dropping_market(monkeypatch, tmp_path):
    configs, _fixed = _scope(tmp_path, monkeypatch, ["us"])
    payload, error = tools.build_tool_payload("receipt_read", {"market": "HK", "deal_id": "complete-id"},
        fixed_input={"config_key": "us", "config_path": str(configs["us"][0])})
    assert payload is None and error is not None
    assert "retry without market" not in error
    assert "clarify" in error


def test_host_routes_hk_tool_and_labels_the_observation(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ["us", "hk"])
    contract = _contract(fixed)
    account = configs["hk"][1]["accounts"][0]
    captured = []

    def read(name, payload, **_kwargs):
        captured.append((name, payload))
        return {"ok": True, "data": {"market": "hk", "account": account, "rows": [],
                                     "returned_count": 0, "next_cursor": None, "evidence_complete": True}}

    monkeypatch.setattr(tools, "call_read_tool", read)
    turns = iter((
        {"message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": "read_hk", "type": "function", "function": {"name": "trade_attribution_read",
                "arguments": json.dumps({"symbol": "3690.HK", "account": account})}}]},
         "finish_reason": "tool_calls"},
        {"message": {"role": "assistant", "content": "HK 本地归属记录的已读页为空。"}, "finish_reason": "stop"},
    ))
    result = run_contract(contract, model_settings=_TEST_MODEL, model_request=lambda **_: next(turns),
                          session_key=session_key_for_contract(contract))
    assert result.ok and result.status == "answered"
    assert len(captured) == 1 and captured[0][1]["config_key"] == "hk"
    observations = [event.payload for event in result.events if event.type == "tool_result"]
    assert observations[0]["host_scope"] == {"tool": "trade_attribution_read", "market": "hk", "account": account}


def test_attribution_symbol_filter_precedes_pagination(monkeypatch, tmp_path):
    from src.application.trades import attribution

    rows = [
        {"open_event_id": event_id, "execution_key": event_id, "status": "pending",
         "contract_key": {"underlying_symbol": symbol}, "evidence_complete": True}
        for event_id, symbol in (("1", "3690.HK"), ("2", "0700.HK"), ("3", "3690.HK"))
    ]
    monkeypatch.setattr(attribution, "attribution_runtime", lambda **_: (None, {}, {"runtime_root": str(tmp_path)}, {}))
    monkeypatch.setattr(attribution, "runtime_config_market", lambda _: "HK")
    monkeypatch.setattr(attribution, "read_trade_attribution_snapshot", lambda *_, **__: [])
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *_, **__: {})
    monkeypatch.setattr(attribution, "combo_reconciliation_mode_for_account", lambda *_, **__: "off")
    monkeypatch.setattr(attribution, "build_trade_attribution_view", lambda *_, **__: {"rows": rows})
    page, _, _ = attribution.trade_attribution_read({"account": "lx", "symbol": "3690.HK", "limit": 1})
    assert page["market"] == "hk" and [row["open_event_id"] for row in page["rows"]] == ["1"]
    cursor = page["next_cursor"]
    assert cursor
    page, _, _ = attribution.trade_attribution_read({"account": "lx", "cursor": cursor})
    assert [row["open_event_id"] for row in page["rows"]] == ["3"]
    with pytest.raises(AgentToolError, match="cursor"):
        attribution.trade_attribution_read({"account": "lx", "symbol": "0700.HK", "cursor": cursor})
    with pytest.raises(AgentToolError, match="cursor"):
        attribution.trade_attribution_read({"account": "sy", "cursor": cursor})
    with pytest.raises(AgentToolError, match="cursor"):
        attribution.trade_attribution_read({"account": "lx", "cursor": cursor + "x"})
    with pytest.raises(AgentToolError, match="无法识别"):
        attribution.trade_attribution_read({"account": "lx", "symbol": "not-a-symbol"})
    with pytest.raises(AgentToolError, match="标的与所选市场不一致"):
        attribution.trade_attribution_read({"account": "lx", "symbol": "NVDA"})
