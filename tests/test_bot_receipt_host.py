"""Receipt acceptance through public Host and real bounded source readers; no provider."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.application.agent_tools import receipts
from src.application.bot.contracts import AppResult, BotRequest, BotScope, new_id
from src.application.bot.host import run_contract
from src.application.bot.scene import build_scene_manifest
from src.application.bot.service import prepare_contract
from src.application.receipt_query import receipt_event
from tests.bot_pi_test_support import _TEST_MODEL

DEAL_ID = "7258806397173991645"  # Sanitized historical fixture, never a live lookup.
BODY = "成交未记录：exception:OperationalError。保留的历史回执没有更具体的异常原因。"
ANSWER = "该成交的历史回执为：" + BODY


@pytest.fixture
def receipt_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    config = tmp_path / "config.hk.json"
    config.write_text(json.dumps({"accounts": ["sy"], "_generated": {"market": "hk", "source_format": "yaml"}}))
    state = tmp_path / "output_shared/state"
    state.mkdir(parents=True)
    ledger = state / "option_positions.sqlite3"
    with sqlite3.connect(ledger) as conn:
        for table, identity in (("trade_lifecycle_notification_outbox", "outbox_id"),
                                ("trade_lifecycle_notification_delivery_batches", "batch_id")):
            conn.execute(f"CREATE TABLE {table} ({identity} TEXT, payload_json TEXT, provider_receipt_json TEXT, created_at_ms INTEGER)")
    inbox = ledger.with_name(ledger.name + ".trade_intake_inbox.sqlite3")
    with sqlite3.connect(inbox) as conn:
        conn.execute("CREATE TABLE trade_inbox(inbox_id TEXT, deal_id TEXT, payload_json TEXT, result_json TEXT, receipt_json TEXT, received_at_ms INTEGER, updated_at_ms INTEGER)")
        for account, deal in (("sy", DEAL_ID), ("lx", "other-account-deal")):
            payload = {"broker_account_ref": {"account_label": account}, "instrument_ref": {"market": "HK", "symbol": "0700.HK"}}
            receipt = {"schema_version": 2, "current_result_key": "manual_required", "receipts": {
                "manual_required": {"receipt_id": "historical-" + account, "message": BODY if account == "sy" else "PRIVATE_OTHER_ACCOUNT",
                                    "business_result": {"reason": "exception:OperationalError"}, "status": "failed"}}}
            conn.execute("INSERT INTO trade_inbox VALUES (?,?,?,?,?,?,?)", (account, deal, json.dumps(payload), "{}", json.dumps(receipt), 1788934554000, 1788934555000))
    snapshots = {path: path.read_bytes() for path in (config, ledger, inbox)}
    yield config
    assert all(path.read_bytes() == original for path, original in snapshots.items())


def _contract(config, *, prior_reference="", user_message=None):
    prepared = prepare_contract(BotRequest(
        request_id=new_id("receipt_test"), source_entry="test", user_message=user_message or f"查询成交 {DEAL_ID} 的历史回执，不推断当前故障原因。{prior_reference}",
        explicit_scope=BotScope(config_key=config.name.split(".")[1], config_path=str(config)),
    ), reference_year=2026)
    assert not isinstance(prepared, AppResult)
    return prepared


def _read(**filters):
    return {"call_id": new_id("read"), "tool_name": "receipt_read", "arguments": {"type": "trade", "deal_id": DEAL_ID, **filters}}


def _run(monkeypatch, contract, turns):
    failures, replies = [], []
    sequence = iter(turns)
    def request(**kwargs):
        try:
            replies[:] = [json.loads(m["content"]) for m in kwargs["messages"] if m["role"] == "tool"]
            result = next(sequence)(tuple(replies))
            if isinstance(result, str):
                return {"message":{"role":"assistant", "content":result}, "finish_reason":"stop"}
            return {"message":{"role":"assistant", "content":"", "tool_calls":[{
                "id":result["call_id"], "type":"function", "function":{"name":result["tool_name"],
                "arguments":json.dumps(result["arguments"])}}]}, "finish_reason":"tool_calls"}
        except Exception as exc:
            failures.append(exc)
            raise
    result = run_contract(contract, model_settings=_TEST_MODEL, model_request=request)
    if failures:
        raise failures[0]
    return result, replies


def _historical_evidence(replies):
    observation = replies[-1]
    assert observation["ok"] is True, observation
    assert observation["data"]["freshness"]["status"] == "historical"
    assert observation["data"]["coverage"]["status"] == "complete"
    row = observation["data"]["rows"][0]
    assert row["deal_id"] == "..." + DEAL_ID[-4:] and row["receipt_body"] == BODY
    assert row["diagnostic_code"] == "exception:OperationalError"
    assert row["body_provenance"] == "frozen" and row["occurred_at"].startswith("2026-09-09")
    assert "PRIVATE_OTHER_ACCOUNT" not in json.dumps(observation)
    return observation["ref"]


def _receipt_answer(replies):
    return ANSWER + f" [{_historical_evidence(replies)}]"


@pytest.mark.parametrize("repeat", range(3))
def test_receipt_reaches_model_and_answer_cites_this_run(receipt_runtime, monkeypatch, repeat):
    result, replies = _run(monkeypatch, _contract(receipt_runtime), [
        lambda _: _read(), _receipt_answer,
    ])
    assert replies[-1]["ref"] in result.user_response
    assert result.ok and result.status == "answered"
    assert ANSWER in result.user_response and "数据库锁" not in result.user_response
    assert any(event.type == "tool_result" for event in result.events)


def test_historical_exception_reaches_model_with_historical_freshness(receipt_runtime, monkeypatch):
    result, replies = _run(monkeypatch, _contract(receipt_runtime), [
        lambda _: _read(), _receipt_answer,
    ])
    assert replies[-1]["data"]["freshness"]["status"] == "historical"
    assert result.ok and BODY in result.user_response
    assert "数据库锁" not in result.user_response


@pytest.mark.parametrize("filter", [{"account": "lx"}, {"market": "US"}, {"config_path": "/tmp/forged.json"}])
def test_receipt_scope_denial_is_not_usable_evidence(receipt_runtime, monkeypatch, filter):
    def denied(replies):
        observation = replies[-1]
        assert observation["ok"] is False
        assert "PRIVATE_OTHER_ACCOUNT" not in json.dumps(observation)
        return "超出本次授权范围，无法核实该回执。"
    result, replies = _run(monkeypatch, _contract(receipt_runtime), [lambda _: _read(**filter), denied])
    assert replies[-1]["ok"] is False
    assert result.ok and result.user_response == "超出本次授权范围，无法核实该回执。"


def test_prior_run_id_quoted_in_new_request_requires_new_read(receipt_runtime, monkeypatch):
    first, replies = _run(monkeypatch, _contract(receipt_runtime), [lambda _: _read(), _receipt_answer])
    assert first.ok
    old_ref = replies[0]["ref"]
    contract = _contract(receipt_runtime, prior_reference=f"上次答案引用 {old_ref}，请继续核实。")
    def cite_new(replies):
        fresh_ref = _historical_evidence(replies)
        assert fresh_ref != old_ref
        return _receipt_answer(replies)
    result, replies = _run(monkeypatch, contract, [lambda _: _read(), cite_new])
    assert result.ok and replies[-1]["ref"] in result.user_response
    assert old_ref not in result.user_response


def _us_receipt_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    config = tmp_path / "config.us.json"
    config.write_text(json.dumps({"accounts": ["lx"], "_generated": {"market": "us", "source_format": "yaml"}}))
    reads = []
    row = receipt_event(source="trade_inbox", event_id="us-fixture", account="lx", market="US",
                        kind="trade", occurred=1788934554000, body="US_RECEIPT_BODY", deal_id=DEAL_ID)
    def sources(**kwargs):
        reads.append(dict(kwargs["query"]))
        return [row], []
    monkeypatch.setattr(receipts, "_sources", sources)
    return config, reads


def test_wrong_model_market_retries_in_fixed_us_scope(tmp_path, monkeypatch):
    config, reads = _us_receipt_runtime(tmp_path, monkeypatch)
    def retry(replies):
        assert reads == []
        assert replies[-1]["ok"] is False
        assert replies[-1]["error"]["code"] == "INPUT_ERROR"
        assert "does not establish" in replies[-1]["error"]["message"]
        return _read(account="lx")
    def answer(replies):
        assert reads == [{"type": "trade", "account": "lx", "deal_id": DEAL_ID, "market": "US"}]
        assert replies[-1]["ok"] is True
        assert replies[-1]["data"]["rows"][0]["receipt_body"] == "US_RECEIPT_BODY"
        return "按 US 范围找到了这笔回执。"
    result, _ = _run(monkeypatch, _contract(config), [lambda _: _read(account="lx", market="HK"), retry, answer])
    assert result.ok and result.user_response == "按 US 范围找到了这笔回执。"


@pytest.mark.parametrize("bad_deal_id", ["..." + DEAL_ID[-4:], "…" + DEAL_ID[-4:]])
def test_masked_deal_id_is_rejected_before_read(tmp_path, monkeypatch, bad_deal_id):
    config, reads = _us_receipt_runtime(tmp_path, monkeypatch)
    def answer(replies):
        assert reads == []
        assert replies[-1]["ok"] is False
        assert replies[-1]["error"]["code"] == "INPUT_ERROR"
        assert "complete deal_id" in replies[-1]["error"]["message"]
        return "需要完整成交编号才能精确核实。"
    result, _ = _run(monkeypatch, _contract(config, user_message=f"请查成交 {bad_deal_id}"),
                     [lambda _: _read(account="lx", deal_id=bad_deal_id), answer])
    assert result.ok and result.user_response == "需要完整成交编号才能精确核实。"


def test_explicit_hk_request_does_not_retry_in_us_scope(tmp_path, monkeypatch):
    config, reads = _us_receipt_runtime(tmp_path, monkeypatch)
    def answer(replies):
        assert reads == []
        assert replies[-1]["error"]["code"] == "INPUT_ERROR"
        return "当前 US 会话无法核实 HK 市场成交。"
    result, _ = _run(monkeypatch, _contract(config, user_message=f"请查 HK 市场成交 {DEAL_ID}"),
                     [lambda _: _read(account="lx", market="HK"), answer])
    assert result.ok and result.user_response == "当前 US 会话无法核实 HK 市场成交。"


@pytest.mark.parametrize("market", ["US", "us"])
def test_same_scope_market_case_is_accepted(tmp_path, monkeypatch, market):
    config, reads = _us_receipt_runtime(tmp_path, monkeypatch)
    result, replies = _run(monkeypatch, _contract(config), [
        lambda _: _read(account="lx", market=market),
        lambda replies: "找到回执。" if replies[-1]["ok"] else "查询失败。",
    ])
    assert result.ok and result.user_response == "找到回执。"
    assert len(reads) == 1 and reads[0]["market"] == "US"


def test_receipt_rules_reach_model_with_fixed_scope(tmp_path, monkeypatch):
    config, _ = _us_receipt_runtime(tmp_path, monkeypatch)
    manifest = build_scene_manifest(_contract(config), new_id("receipt_scene"))
    prompt = "\n".join(message["content"] for message in manifest.messages if message["role"] == "system")
    assert manifest.fixed_tool_input["config_key"] == "us"
    assert "Hong Kong time or broker location is not evidence" in prompt
    assert "do not retry by omitting market" in prompt
    assert "Never drop deal_id" in prompt
    assert "not the receipt's actual market" in prompt
