from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from src.application.agent_tool_contracts import AgentToolError

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_execution import execution_identity_from_input
from src.application.bot.control import attribution_operations as operations
from src.application.bot.control.contracts import BotInboundRequest, ControlCommand
from src.application.bot.control.command_parser import parse_bot_control_command
from src.application.bot.control.inbound_control import execute_explicit_control
from src.application.bot.control.operation_store import InboundOperationStore
from src.application.bot.control.permission_response import parse_permission_response
from src.application.ledger.api import ledger_resource_identity
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object


@pytest.fixture
def attribution_context(tmp_path, monkeypatch):
    for key, value in {"OM_INBOUND_OPERATIONS_ENABLED": "1", "OM_INBOUND_TRADE_WRITE_ENABLED": "1",
                       "OM_INBOUND_ADMIN_OPEN_IDS": "wechat:user,feishu:user",
                       "OM_INBOUND_OPERATION_HMAC_KEY": "isolated-test-key"}.items():
        monkeypatch.setenv(key, value)
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    execution = {"external_id_namespace": "futu.deal", "external_execution_id": "123",
                 "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}}
    execution_key = execution_identity_from_input(execution)
    persist_trade_event_object(repo, TradeEvent(
        event_id="fill", event_type="open", event_time_ms=1000,
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="NVDA",
                                            option_type="put", strike=100, expiration_ymd="2026-12-18"),
        contracts=1, price=2, multiplier=100, currency="USD", source="futu", lot_id="lot",
        raw_payload={"side": "sell", "execution_input": execution, "execution_id": execution_key}))
    authority = {"config_path": str(tmp_path / "config.us.json"), "runtime_root": str(tmp_path),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": "fixture"}
    mapping = {"physical_account_ids": ["1001"], "environment": "REAL"}
    config = {"market": "us", "accounts": ["lx"],
              "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}
    monkeypatch.setattr(operations, "attribution_runtime", lambda **_: (repo, config, authority.copy(), mapping.copy()))
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: {})
    monkeypatch.setattr(operations, "read_attribution_combo_evidence", lambda *a, **k: {"complete": True, "exposures": []})
    monkeypatch.setattr("src.application.trades.attribution.trade_attribution_capacity_check",
                        lambda **_: {"status": "available", "reason_codes": []})
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    request = BotInboundRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room",
                               config_key="us")
    preview = ControlCommand("attribution_preview", {"account": "lx", "execution_key": execution_key, "action": "ordinary"})
    return repo, store, request, preview, authority


@pytest.mark.parametrize("channel", ["wechat", "feishu"])
def test_attribution_control_preview_confirm_retry_and_wrong_conversation(attribution_context, channel):
    repo, store, request, preview, _ = attribution_context
    request = replace(request, channel=channel)
    result = execute_explicit_control(preview, request=request, command_id="in_preview", operation_store=store)
    assert result.ok and result.requires_confirmation
    assert "NVDA 2026-12-18 100 Put" in result.response_text
    assert "卖出开仓 1 张" in result.response_text and "USD 200.00" in result.response_text
    assert "待归属 → 普通单腿" in result.response_text
    assert len(repo.list_trade_events()) == 1
    confirmed = parse_permission_response("确认", request=request, store=store)
    assert confirmed.intent_name == "attribution_confirm"
    with pytest.raises(AgentToolError, match="原对话"):
        execute_explicit_control(confirmed, request=replace(request, conversation_id="other"),
                                 command_id="in_wrong", operation_store=store)
    assert len(repo.list_trade_events()) == 1
    applied = execute_explicit_control(confirmed, request=request, command_id="in_confirm", operation_store=store)
    assert applied.ok
    assert store.get("in_preview")["status"] == "applied"
    retry = parse_bot_control_command("/confirm attribution in_preview")
    assert execute_explicit_control(retry, request=request, command_id="in_retry", operation_store=store).ok
    assert len(repo.list_trade_events()) == 2


def test_recovery_fails_uncommitted_claim_and_blocks_delayed_worker(attribution_context):
    repo, store, request, preview, _ = attribution_context
    operations.handle_attribution_operation(preview, request, command_id="in_preview", store=store)
    operation = store.get("in_preview")
    store.mark_confirmed("in_preview", expected_payload_hash=operation["payload_hash"])
    result = operations.recover_attribution_operations(config_key="us", config_path=None, store=store)
    assert result["failed"] == 1
    late = operations._finish_attribution_operation(repo, store=store, operation=operation, apply_changes=True)
    assert late["data"]["status"] == "failed"
    assert len(repo.list_trade_events()) == 1


def test_preview_exceeding_channel_limit_is_cancelled(attribution_context):
    repo, store, request, preview, _ = attribution_context
    request = replace(request, reply_context={"max_reply_chars": 40})
    with pytest.raises(AgentToolError, match="完整归属预览"):
        operations.handle_attribution_operation(preview, request, command_id="too-long", store=store)
    assert store.get("too-long")["status"] == "cancelled"
    result = operations.handle_attribution_operation(
        ControlCommand("attribution_confirm", {"operation_id": "too-long"}),
        request, command_id="confirm-too-long", store=store)
    assert result["data"]["status"] == "cancelled"
    assert len(repo.list_trade_events()) == 1


def test_recovery_repairs_committed_effect_after_expired_claim(attribution_context, monkeypatch):
    repo, store, request, preview, _ = attribution_context
    operations.handle_attribution_operation(preview, request, command_id="in_preview", store=store)
    original_mark = store.mark_applied
    monkeypatch.setattr(store, "mark_applied", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
    with pytest.raises(RuntimeError, match="audit unavailable"):
        operations.handle_attribution_operation(ControlCommand("attribution_confirm", {"operation_id": "in_preview"}),
                                                request, command_id="in_confirm", store=store)
    assert len(repo.list_trade_events()) == 2
    assert store.get("in_preview")["status"] == "confirmed"
    monkeypatch.setattr(store, "mark_applied", original_mark)
    store.reconcile_stale_operations(now=datetime.now(timezone.utc) + timedelta(days=1))
    assert store.get("in_preview")["status"] == "confirmed"
    result = operations.recover_attribution_operations(config_key="us", config_path=None, store=store)
    assert result["applied"] == 1
    assert store.get("in_preview")["status"] == "applied"
    assert len(repo.list_trade_events()) == 2


def test_cancel_readback_error_keeps_committed_decision_recoverable(attribution_context, monkeypatch):
    import sqlite3

    repo, store, request, preview, _ = attribution_context
    operations.handle_attribution_operation(preview, request, command_id="in_preview", store=store)
    original_mark = store.mark_applied
    monkeypatch.setattr(store, "mark_applied", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
    with pytest.raises(RuntimeError, match="audit unavailable"):
        operations.handle_attribution_operation(parse_bot_control_command("/confirm attribution in_preview"),
                                                request, command_id="in_confirm", store=store)
    monkeypatch.setattr(store, "mark_applied", original_mark)
    assert store.get("in_preview")["status"] == "confirmed" and len(repo.list_trade_events()) == 2
    original_read = operations._read_decision
    monkeypatch.setattr(operations, "_read_decision", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("readback unavailable")))
    with pytest.raises(sqlite3.OperationalError, match="readback unavailable"):
        operations.handle_attribution_operation(parse_bot_control_command("/cancel attribution in_preview"),
                                                request, command_id="in_cancel", store=store)
    assert store.get("in_preview")["status"] == "confirmed"
    monkeypatch.setattr(operations, "_read_decision", original_read)
    recovered = operations.recover_attribution_operations(config_key="us", config_path=None, store=store)
    assert recovered["applied"] == 1 and store.get("in_preview")["status"] == "applied"
    assert len(repo.list_trade_events()) == 2


def test_confirm_rejects_changed_resource_without_mutation(attribution_context):
    repo, store, request, preview, authority = attribution_context
    operations.handle_attribution_operation(preview, request, command_id="in_preview", store=store)
    authority["account_mapping_hash"] = "rebound"
    with pytest.raises(AgentToolError, match="账户映射"):
        execute_explicit_control(ControlCommand("attribution_confirm", {"operation_id": "in_preview"}),
                                 request=request, command_id="in_confirm", operation_store=store)
    assert store.get("in_preview")["status"] == "previewed"
    assert len(repo.list_trade_events()) == 1


def test_bot_wheel_manual_membership_and_commit_recovery(tmp_path, monkeypatch):
    from test_trade_attribution_view import _writable_call_scope
    from src.application.ledger.api import read_trade_attribution_facts
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    config["accounts"] = ["lx"]
    for key, value in {"OM_INBOUND_OPERATIONS_ENABLED": "1", "OM_INBOUND_TRADE_WRITE_ENABLED": "1",
                       "OM_INBOUND_ADMIN_OPEN_IDS": "wechat:user", "OM_INBOUND_OPERATION_HMAC_KEY": "isolated-key"}.items():
        monkeypatch.setenv(key, value)
    authority = {"config_path": str(tmp_path / "config.us.json"), "runtime_root": str(tmp_path),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": "fixture"}
    monkeypatch.setattr(operations, "attribution_runtime", lambda **_: (repo, config, authority,
        {"physical_account_ids": ["1001"], "environment": "REAL"}))
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: {})
    monkeypatch.setattr(operations, "read_attribution_combo_evidence", lambda *a, **k: {"complete": True, "exposures": []})
    fact = next(row for row in read_trade_attribution_facts(repo, account="lx") if row["contract_key"]["option_type"] == "call")
    from src.application.wheel.read_model import build_wheel_read_model
    target = build_wheel_read_model(repo, "lx", 4000, market="us")["wheel_branches"][0]["wheel_branch_id"]
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    request = BotInboundRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room", config_key="us")
    preview = ControlCommand("attribution_preview", {"account": "lx", "execution_key": fact["execution_key"],
        "action": "wheel", "target_id": target})
    rendered = operations.handle_attribution_operation(preview, request, command_id="wheel-preview", store=store)["data"]["response_text"]
    assert "Call" in rendered and "卖出开仓 1 张" in rendered and f"→ Wheel 批次 {target}" in rendered
    assert fact["execution_key"] in rendered and "当前提示｜" in rendered
    payload = store.get("wheel-preview")["payload"]
    assert payload["member_ids"] == [fact["lot_id"]] and payload["candidate_id"] == "wheel:" + target
    original_mark = store.mark_applied
    monkeypatch.setattr(store, "mark_applied", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
    with pytest.raises(RuntimeError, match="audit unavailable"):
        operations.handle_attribution_operation(ControlCommand("attribution_confirm", {"operation_id": "wheel-preview"}),
            request, command_id="confirm", store=store)
    after = next(row for row in read_trade_attribution_facts(repo, account="lx") if row["lot_id"] == fact["lot_id"])
    assert after["status"] == "linked" and after["origin"] == "manual"
    monkeypatch.setattr(store, "mark_applied", original_mark)
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: pytest.fail("recovery must not query provider"))
    assert operations.recover_attribution_operations(config_key="us", config_path=None, store=store)["applied"] == 1
    assert store.get("wheel-preview")["status"] == "applied"
    count = len(repo.list_trade_events())
    result = operations.handle_attribution_operation(ControlCommand("attribution_confirm", {"operation_id": "wheel-preview"}),
        request, command_id="retry", store=store)
    assert result["data"]["status"] == "applied" and len(repo.list_trade_events()) == count


def test_bot_confirms_three_contracts_to_three_wheel_branches(tmp_path, monkeypatch):
    from test_trade_attribution_meituan import _meituan_repo
    from src.application.ledger.api import read_trade_attribution_facts
    from src.application.wheel.read_model import build_wheel_read_model
    repo, config, _ = _meituan_repo(tmp_path, call_contracts=3)
    for key, value in {"OM_INBOUND_OPERATIONS_ENABLED": "1", "OM_INBOUND_TRADE_WRITE_ENABLED": "1",
                       "OM_INBOUND_ADMIN_OPEN_IDS": "wechat:user", "OM_INBOUND_OPERATION_HMAC_KEY": "isolated-key"}.items():
        monkeypatch.setenv(key, value)
    authority = {"config_path": str(tmp_path / "config.hk.json"), "runtime_root": str(tmp_path),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": "fixture"}
    monkeypatch.setattr(operations, "attribution_runtime", lambda **_: (repo, config, authority,
        {"physical_account_ids": ["1001"], "environment": "REAL"}))
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: {})
    monkeypatch.setattr(operations, "read_attribution_combo_evidence",
                        lambda *a, **k: {"complete": True, "exposures": []})
    monkeypatch.setattr("src.application.trades.attribution.trade_attribution_capacity_check",
                        lambda **_: {"status": "available", "reason_codes": []})
    fact = next(row for row in read_trade_attribution_facts(repo, account="lx")
                if row["contract_key"]["option_type"] == "call")
    branches = build_wheel_read_model(repo, "lx", 10**16, market="hk")["wheel_branches"]
    chosen = sorted(row["wheel_branch_id"] for row in branches)[:3]
    command = parse_bot_control_command(
        f"/attribute lx {fact['execution_key']} wheel {','.join(chosen)}")
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    request = BotInboundRequest(text="归属", sender_id="user", channel="wechat",
                               conversation_id="room", config_key="hk")
    preview = operations.handle_attribution_operation(command, request, command_id="multi-preview", store=store)
    assert all(branch in preview["data"]["response_text"] for branch in chosen)
    assert preview["data"]["response_text"].count("：1 张") == 3
    assert len(store.get("multi-preview")["payload"]["wheel_call_allocations"]) == 3
    confirmed = operations.handle_attribution_operation(
        ControlCommand("attribution_confirm", {"operation_id": "multi-preview"}),
        request, command_id="multi-confirm", store=store)
    assert confirmed["data"]["status"] == "applied"
    linked = next(row for row in read_trade_attribution_facts(repo, account="lx")
                  if row["execution_key"] == fact["execution_key"])
    assert linked["status"] == "linked" and len(linked["wheel_call_allocations"]) == 3
    count = len(repo.list_trade_events())
    assert operations.handle_attribution_operation(
        ControlCommand("attribution_confirm", {"operation_id": "multi-preview"}),
        request, command_id="multi-retry", store=store)["data"]["status"] == "applied"
    assert len(repo.list_trade_events()) == count


def test_bot_combo_freezes_both_members_and_confirms_atomic_pair(tmp_path, monkeypatch):
    from test_combo_reconciliation_application import _call_open, _put_open, BASE_TIME_MS
    from src.application.ledger.api import read_trade_attribution_snapshot, read_trade_attribution_facts
    from src.application.trades.attribution import build_trade_attribution_view
    repo = SQLiteOptionPositionsRepository(tmp_path / "combo.sqlite3")
    for event in (_call_open(), _put_open()):
        execution = {**event.raw_payload["execution_input"], "external_id_namespace": "futu.deal", "external_execution_id": event.event_id}
        persist_trade_event_object(repo, replace(event, raw_payload={**event.raw_payload, "execution_input": execution,
            "execution_id": execution_identity_from_input(execution)}))
    config = {"accounts": ["lx"], "market": "us", "trade_intake": {"combo_reconciliation": {"accounts": {"lx": "confirm"}}},
              "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}
    for key, value in {"OM_INBOUND_OPERATIONS_ENABLED": "1", "OM_INBOUND_TRADE_WRITE_ENABLED": "1",
                       "OM_INBOUND_ADMIN_OPEN_IDS": "wechat:user", "OM_INBOUND_OPERATION_HMAC_KEY": "isolated-key"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("time.time", lambda: (BASE_TIME_MS + 3000) / 1000)
    monkeypatch.setattr("src.application.trades.attribution.trade_attribution_capacity_check", lambda **_: {"status": "available", "reason_codes": []})
    authority = {"config_path": str(tmp_path / "config.us.json"), "runtime_root": str(tmp_path),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": "fixture"}
    monkeypatch.setattr(operations, "attribution_runtime", lambda **_: (repo, config, authority,
        {"physical_account_ids": ["1001"], "environment": "REAL"}))
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: {})
    monkeypatch.setattr(operations, "read_attribution_combo_evidence", lambda *a, **k: {"complete": True, "exposures": []})
    rows = read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = build_trade_attribution_view(rows, config=config, account="lx", market="us", now_ms=BASE_TIME_MS + 3000,
                                       combo_evidence={"complete": True}, combo_mode="confirm")
    fact = view["rows"][0]
    assert len(fact["candidates"]) == 1, fact
    target = fact["candidates"][0]["candidate_id"]
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    request = BotInboundRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room", config_key="us")
    preview_result = operations.handle_attribution_operation(ControlCommand("attribution_preview", {"account": "lx",
        "execution_key": fact["execution_key"], "action": "combo", "target_id": target}), request, command_id="combo-preview", store=store)
    rendered = preview_result["data"]["response_text"]
    assert "第 1 腿｜" in rendered and "第 2 腿｜" in rendered
    assert "Put" in rendered and "Call" in rendered and "→ Combo Yield " in rendered
    assert "买入开仓" in rendered and "卖出开仓" in rendered
    assert all(row["execution_key"] in rendered for row in view["rows"])
    assert sorted(store.get("combo-preview")["payload"]["member_ids"]) == ["call-lot", "put-lot"]
    assert len(repo.list_trade_events()) == 2
    result = operations.handle_attribution_operation(ControlCommand("attribution_confirm", {"operation_id": "combo-preview"}),
        request, command_id="confirm", store=store)
    assert result["data"]["status"] == "applied", result
    facts = read_trade_attribution_facts(repo, account="lx")
    assert all(row["origin"] == "manual" and row["status"] == "linked" for row in facts)
    assert len(repo.list_trade_events()) == 4


def test_real_bot_scene_reads_then_hands_off_preview_without_confirmation(attribution_context, monkeypatch, example_config_path):
    import json
    from src.application.bot.contracts import BotRequest, BotScope
    from src.application.bot.service import prepare_contract
    from src.application.bot import tools as bot_tools
    from src.application.bot.control.capability_catalog import preview_operation_capabilities
    from tests.test_bot_python_runtime import MODEL, run_contract, call, answer, script
    repo, store, request, preview, _ = attribution_context
    seen = []
    def read(name, payload, **kwargs):
        seen.append(name)
        assert name == "trade_attribution_read"
        return {"ok": True, "data": {"rows": [{"execution_key": preview.arguments["execution_key"], "status": "pending"}]}}
    monkeypatch.setattr(bot_tools, "call_read_tool", read)
    contract = prepare_contract(BotRequest(request_id="bot-attribution", source_entry="test", user_message="将这笔成交确认为普通单腿",
        explicit_scope=BotScope(config_path=str(example_config_path))))
    captured = []
    result = run_contract(contract, model_settings=MODEL, control_preview_specs=preview_operation_capabilities(), model_request=script([
        call("trade_attribution_read", {"account": "lx"}),
        call("request_control_preview", {"intent_name": "attribution_preview", "arguments": preview.arguments}, id="preview"),
        answer("生成预览")], captured))
    assert result.ok and result.status == "control_requested", result
    assert seen == ["trade_attribution_read"]
    assert len(repo.list_trade_events()) == 1
    command = ControlCommand(**result.control_request)
    response = execute_explicit_control(command, request=request, command_id="bot-preview", operation_store=store)
    assert response.requires_confirmation and len(repo.list_trade_events()) == 1
    rejected = run_contract(contract, model_settings=MODEL, control_preview_specs=preview_operation_capabilities(), model_request=script([
        call("request_control_preview", {"intent_name": "attribution_confirm", "arguments": {"operation_id": "bot-preview"}}),
        answer("需要本人确认")], captured))
    assert rejected.status == "answered" and rejected.control_request is None
    assert json.loads(captured[-1]["messages"][-1]["content"])["ok"] is False
    assert len(repo.list_trade_events()) == 1


@pytest.mark.parametrize("conversation", [None, "", " ", " room ", "room"])
def test_public_attribution_facade_preserves_original_conversation(attribution_context, conversation):
    from src.application.bot.control.inbound_service import handle_bot_request
    from src.application.bot.control.audit import InboundAuditStore
    repo, store, request, preview, _ = attribution_context
    audit = InboundAuditStore(store.path)
    result = handle_bot_request(replace(request, message_id="preview", conversation_id=conversation,
        text=f"/attribute lx {preview.arguments['execution_key']} ordinary"), audit_store=audit, allowed_senders="wechat:user")
    assert result["ok"] is (conversation == "room"), result
    assert len(repo.list_trade_events()) == 1
    if conversation == "room":
        operation_id = result["data"]["command_id"]
        for index, invalid in enumerate([None, "", " ", " room "]):
            rejected = handle_bot_request(replace(request, message_id=f"confirm-{index}", conversation_id=invalid,
                text=f"/confirm attribution {operation_id}"), audit_store=audit, allowed_senders="wechat:user")
            assert not rejected["ok"], rejected
        assert store.get(operation_id)["status"] == "previewed"
        applied = handle_bot_request(replace(request, message_id="valid-confirm",
            text=f"/confirm attribution {operation_id}"), audit_store=audit, allowed_senders="wechat:user")
        assert applied["ok"] and len(repo.list_trade_events()) == 2
    else:
        assert store.get(result["data"]["command_id"]) is None


@pytest.mark.parametrize("failure", ["cancel", "persistence"])
def test_bot_handoff_is_discarded_after_terminal_failure(attribution_context, monkeypatch, example_config_path, tmp_path, failure):
    from src.application.bot.contracts import BotRequest, BotScope
    from src.application.bot.service import prepare_contract
    from src.application.bot.host_store import BotHostStore
    from src.application.bot.control.capability_catalog import preview_operation_capabilities
    from src.application.bot.control import inbound_service
    from src.application.bot.control.audit import InboundAuditStore
    from tests.test_bot_python_runtime import MODEL, run_contract, call, answer, script
    repo, store, request, preview, _ = attribution_context
    host = BotHostStore(tmp_path / "host.sqlite3")
    if failure == "cancel":
        original = host.claim_admission_decision
        def cancel(run_id, wanted):
            host.request_cancel(run_id)
            return original(run_id, wanted)
        monkeypatch.setattr(host, "claim_admission_decision", cancel)
    else:
        monkeypatch.setattr(host, "finish_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk unavailable")))
    contract = prepare_contract(BotRequest(request_id="bot-failure", source_entry="test", user_message="确认这笔成交为普通单腿",
        explicit_scope=BotScope(config_path=str(example_config_path))))
    result = run_contract(contract, model_settings=MODEL, host_store=host,
        control_preview_specs=preview_operation_capabilities(), model_request=script([
            call("request_control_preview", {"intent_name": "attribution_preview", "arguments": preview.arguments}), answer("生成预览")], []))
    assert not result.ok and result.control_request is None
    assert result.status == ("cancelled" if failure == "cancel" else "failed")
    # The facade also refuses stale handoffs from a failed host result.
    monkeypatch.setattr(inbound_service, "_run_bot", lambda *a, **k: replace(result,
        control_request={"intent_name": "attribution_preview", "arguments": preview.arguments}))
    response = inbound_service.handle_bot_request(replace(request, text="归属这笔订单", message_id="failed-bot"),
        audit_store=InboundAuditStore(store.path), allowed_senders="wechat:user")
    assert not response["ok"] and store.get(response["data"]["command_id"]) is None
    assert len(repo.list_trade_events()) == 1


def test_confirm_and_cancel_read_current_effect_after_void(attribution_context):
    import time
    repo, store, request, preview, _ = attribution_context
    operations.handle_attribution_operation(preview, request, command_id="preview", store=store)
    operations.handle_attribution_operation(ControlCommand("attribution_confirm", {"operation_id": "preview"}),
        request, command_id="confirm", store=store)
    adjustment = next(event for event in repo.list_trade_events() if event["event_type"] == "adjust")
    persist_trade_event_object(repo, TradeEvent(event_id="void-attribution", event_type="void",
        event_time_ms=int(time.time() * 1000) + 1, contract_key=ContractKey.from_values(**adjustment["contract_key"]),
        contracts=0, price=0, currency="USD", multiplier=100, source="test_repair", target_event_id=adjustment["event_id"]))
    for action in ("attribution_confirm", "attribution_cancel"):
        result = operations.handle_attribution_operation(ControlCommand(action, {"operation_id": "preview"}),
            request, command_id=action, store=store)
        assert not result["ok"] and result["data"]["status"] == "conflict", result
        assert "attribution_effect_changed" in result["data"]["result"]["reason_codes"]
    assert len(repo.list_trade_events()) == 3


@pytest.mark.parametrize("state", ["confirmed", "running"])
@pytest.mark.parametrize("expired", [False, True])
def test_claimed_confirmation_is_readback_only(attribution_context, monkeypatch, state, expired):
    from src.application.bot.control import operation_store
    repo, store, request, preview, _ = attribution_context
    with monkeypatch.context() as clock:
        if expired:
            clock.setattr(operation_store, "utc_now_iso", lambda: "2020-01-01T00:00:00+00:00")
        operations.handle_attribution_operation(preview, request, command_id="preview", store=store)
        op = store.get("preview")
        assert store.mark_confirmed("preview", expected_payload_hash=op["payload_hash"])
        if state == "running":
            assert store.mark_running("preview", result={})
    result = operations.handle_attribution_operation(ControlCommand("attribution_confirm", {"operation_id": "preview"}),
        request, command_id="retry", store=store)
    assert not result["ok"] and result["data"]["status"] == "failed", result
    assert len(repo.list_trade_events()) == 1


def test_confirm_cas_loser_never_applies(attribution_context, monkeypatch):
    repo, store, request, preview, _ = attribution_context
    operations.handle_attribution_operation(preview, request, command_id="preview", store=store)
    original = store.mark_confirmed
    def competing_claim(*args, **kwargs):
        assert original(*args, **kwargs)
        return False
    monkeypatch.setattr(store, "mark_confirmed", competing_claim)
    result = operations.handle_attribution_operation(ControlCommand("attribution_confirm", {"operation_id": "preview"}),
        request, command_id="retry", store=store)
    assert not result["ok"] and len(repo.list_trade_events()) == 1


@pytest.mark.parametrize("source", ["pending", "permission"])
@pytest.mark.parametrize("action", ["confirm", "cancel"])
def test_generated_attribution_commands_roundtrip_public_facade(attribution_context, source, action):
    import re
    from src.application.bot.control.inbound_service import handle_bot_request
    from src.application.bot.control.audit import InboundAuditStore
    repo, store, request, preview, _ = attribution_context
    audit = InboundAuditStore(store.path)
    def send(text, message):
        return handle_bot_request(replace(request, text=text, message_id=message),
            audit_store=audit, allowed_senders="wechat:user")
    created = send(f"/attribute lx {preview.arguments['execution_key']} ordinary", "preview")
    assert created["ok"]
    if source == "permission":
        command = created["data"]["permission_request"][action + "_hint"]
    else:
        pending = send("/pending", "pending")
        assert pending["ok"]
        text = pending["data"]["response_text"]
        assert "成交策略归属" in text and "NVDA" in text and "普通单腿" in text
        command = re.search(r"/" + action + r" attribution \S+", text).group()
    result = send(command, "action")
    assert result["ok"], result
    assert store.get(created["data"]["operation_id"])["status"] == ("applied" if action == "confirm" else "cancelled")
    assert len(repo.list_trade_events()) == (2 if action == "confirm" else 1)


@pytest.mark.parametrize("payload", [
    '{"conflict_event_ids":["c"],"members":[],"members":[]}',
    '{"conflict_event_ids":["c","c"],"members":[{"execution_key":"e","action":"ordinary"}]}',
    '{"conflict_event_ids":["c"],"members":[{"execution_key":"e","action":"ordinary","target_id":"w"}]}',
    '{"conflict_event_ids":["c"],"members":[{"execution_key":"e","action":"wheel","wheel_call_allocations":[{"stock_lot_id":"s","wheel_branch_id":"b","contracts":true}]}]}',
    '{"conflict_event_ids":["c"],"members":[{"execution_key":"e","action":"ordinary","actor":"forged"}]}',
    '{"conflict_event_ids":["c"],"members":[{"execution_key":"e","action":"ordinary"},{"execution_key":"e","action":"ordinary"}]}',
])
def test_batch_command_rejects_ambiguous_or_forged_input(payload):
    with pytest.raises(AgentToolError, match="批量归属输入无效"):
        parse_bot_control_command("/attribute lx batch '" + payload + "'")


@pytest.mark.parametrize("explicit_lot", [False, True])
@pytest.mark.parametrize("explicit_allocation", [False, True])
def test_batch_control_complete_proof_recovery_and_late_cancel(tmp_path, monkeypatch, explicit_lot, explicit_allocation):
    import json
    from copy import deepcopy
    from test_attribution_conflict_decision import _scope, _view, _conflict, _statuses
    from src.application.ledger.api import read_trade_attribution_facts
    repo, config = _scope(tmp_path, monkeypatch)
    source = next(row for row in repo.list_trade_events() if row["event_type"] == "open" and row["option_type"] == "call")
    raw = {"side": "sell", "execution_input": deepcopy(source["raw_payload"]["execution_input"])}
    raw["execution_input"]["external_execution_id"] = "batch-second"
    raw["execution_id"] = execution_identity_from_input(raw["execution_input"])
    persist_trade_event_object(repo, replace(TradeEvent.from_dict(source), event_id="batch-second",
        lot_id="lot_batch-second" if explicit_lot else None, raw_payload=raw))
    calls = [row for row in _view(repo, config)["rows"] if row["contract_key"]["option_type"] == "call"]
    conflict = _conflict(repo, _view(repo, config), keys=tuple(row["execution_key"] for row in calls))
    for key, value in {"OM_INBOUND_OPERATIONS_ENABLED": "1", "OM_INBOUND_TRADE_WRITE_ENABLED": "1",
        "OM_INBOUND_ADMIN_OPEN_IDS": "wechat:user", "OM_INBOUND_OPERATION_HMAC_KEY": "isolated-key"}.items():
        monkeypatch.setenv(key, value)
    authority = {"config_path": str(tmp_path / "config.hk.json"), "runtime_root": str(tmp_path),
        "ledger": ledger_resource_identity(repo), "account_mapping_hash": "fixture"}
    monkeypatch.setattr(operations, "attribution_runtime", lambda **_: (repo, config, authority,
        {"physical_account_ids": ["1001"], "environment": "REAL"}))
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: {})
    monkeypatch.setattr(operations, "read_attribution_combo_evidence", lambda *a, **k: {"complete": True, "exposures": []})
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    request = BotInboundRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room", config_key="hk")
    payload = {"conflict_event_ids": [conflict], "members": [
        {"execution_key": calls[0]["execution_key"], "action": "ordinary"},
        {"execution_key": calls[1]["execution_key"], "action": "wheel", "target_id": calls[1]["candidate_ids"][0]}]}
    if explicit_allocation:
        member = payload["members"][1]
        branch_id = member.pop("target_id").removeprefix("wheel:")
        branch = next(row for row in _view(repo, config)["wheel_model"]["wheel_branches"] if row["wheel_branch_id"] == branch_id)
        member["wheel_call_allocations"] = [{"stock_lot_id": branch["stock_lot_id"],
            "wheel_branch_id": branch_id, "contracts": calls[1]["contracts"]}]
    before = repo.list_trade_events()
    command = parse_bot_control_command("/attribute lx batch '" + json.dumps(payload) + "'")
    preview = execute_explicit_control(command, request=request, command_id="in_batch", operation_store=store)
    assert preview.ok and preview.requires_confirmation, preview
    assert "第 2 腿" in preview.response_text and conflict in preview.response_text
    assert "→ 普通单腿" in preview.response_text and "→ Wheel" in preview.response_text
    assert repo.list_trade_events() == before
    mark = store.mark_applied
    monkeypatch.setattr(store, "mark_applied", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
    with pytest.raises(RuntimeError, match="audit unavailable"):
        operations.handle_attribution_operation(parse_bot_control_command("/confirm attribution in_batch"), request,
            command_id="in_confirm", store=store)
    assert len(repo.list_trade_events()) == len(before) + 2 and _statuses(repo)[conflict]["resolved"]
    monkeypatch.setattr(store, "mark_applied", mark)
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: (_ for _ in ()).throw(AssertionError("recovery must not query broker")))
    assert operations.recover_attribution_operations(config_key="hk", config_path=None, store=store)["applied"] == 1
    for verb in ("cancel", "confirm"):
        result = operations.handle_attribution_operation(parse_bot_control_command(f"/{verb} attribution in_batch"),
            request, command_id="in_retry", store=store)
        assert result["data"]["status"] == "applied" and len(result["data"]["result"]["proof_event_ids"]) == 2
    assert len(repo.list_trade_events()) == len(before) + 2
    assert len([row for row in read_trade_attribution_facts(repo, account="lx") if row["origin"] == "manual"]) == 2


@pytest.mark.parametrize("change", ["expire", "permission", "config", "cancel"])
def test_control_rechecks_authority_after_writer_and_rolls_back(attribution_context, monkeypatch, change):
    from src.application.trades import attribution
    repo, store, request, preview, authority = attribution_context
    operations.handle_attribution_operation(preview, request, command_id="in_preview", store=store)
    writer = attribution.write_trade_attribution_decision
    before = repo.list_trade_events()
    def changed(*args, **kwargs):
        result = writer(*args, **kwargs)
        if change == "expire":
            monkeypatch.setattr(operations, "operation_is_expired", lambda _: True)
        elif change == "permission":
            monkeypatch.setenv("OM_INBOUND_TRADE_WRITE_ENABLED", "0")
        elif change == "config":
            authority["account_mapping_hash"] = "changed"
        else:
            operation = store.get("in_preview")
            store.mark_cancelled("in_preview", result={"status": "cancelled"}, expected_payload_hash=operation["payload_hash"],
                expected_statuses=("previewed", "confirmed", "running"))
        return result
    monkeypatch.setattr(attribution, "write_trade_attribution_decision", changed)
    result = operations.handle_attribution_operation(parse_bot_control_command("/confirm attribution in_preview"),
        request, command_id="in_confirm", store=store)
    assert result["data"]["status"] == ("cancelled" if change == "cancel" else "failed") and repo.list_trade_events() == before


def test_recovery_does_not_adopt_another_requests_same_result(attribution_context):
    repo, store, request, preview, _ = attribution_context
    for name in ("in_first", "in_other"):
        operations.handle_attribution_operation(preview, request, command_id=name, store=store)
    operations.handle_attribution_operation(parse_bot_control_command("/confirm attribution in_other"), request,
        command_id="in_confirm", store=store)
    first = store.get("in_first")
    store.mark_confirmed("in_first", expected_payload_hash=first["payload_hash"])
    recovered = operations.recover_attribution_operations(config_key="us", config_path=None, store=store)
    assert recovered["failed"] == 1 and store.get("in_first")["status"] == "failed"
    assert store.get("in_other")["status"] == "applied" and len(repo.list_trade_events()) == 2


def test_batch_size_is_rejected_not_truncated():
    import json
    payload = {"conflict_event_ids": ["c" * 17000], "members": [{"execution_key": "e", "action": "ordinary"}]}
    with pytest.raises(AgentToolError, match="16 KiB"):
        parse_bot_control_command("/attribute lx batch '" + json.dumps(payload) + "'")


def test_allocation_quantity_is_checked_before_expansion(attribution_context):
    import json
    repo, store, request, preview, _ = attribution_context
    payload = {"conflict_event_ids": ["c"], "members": [{"execution_key": preview.arguments["execution_key"],
        "action": "wheel", "wheel_call_allocations": [{"stock_lot_id": "s", "wheel_branch_id": "b", "contracts": 10**12}]}]}
    command = parse_bot_control_command("/attribute lx batch '" + json.dumps(payload) + "'")
    with pytest.raises(AgentToolError, match="分配张数与原成交不一致"):
        operations.handle_attribution_operation(command, request, command_id="oversized-allocation", store=store)
    assert store.get("oversized-allocation") is None and len(repo.list_trade_events()) == 1


@pytest.mark.parametrize("decision", ["keep", "move", "ordinary"])
def test_conflict_preview_identifies_released_and_target_branch(tmp_path, monkeypatch, decision):
    from test_attribution_conflict_decision import _scope, _view, _call, _args, _conflict
    from src.application.trades import attribution
    repo, config = _scope(tmp_path, monkeypatch)
    fact = _call(_view(repo, config))
    old_target = fact["candidate_ids"][0]
    attribution.apply_trade_attribution(repo, **_args(config, fact, old_target, "initial"))
    _conflict(repo, _view(repo, config))
    fact = _call(_view(repo, config))
    for key, value in {"OM_INBOUND_OPERATIONS_ENABLED": "1", "OM_INBOUND_TRADE_WRITE_ENABLED": "1",
                       "OM_INBOUND_ADMIN_OPEN_IDS": "wechat:user", "OM_INBOUND_OPERATION_HMAC_KEY": "isolated"}.items():
        monkeypatch.setenv(key, value)
    authority = {"config_path": str(tmp_path / "config.hk.json"), "runtime_root": str(tmp_path),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": "fixture"}
    monkeypatch.setattr(operations, "attribution_runtime", lambda **_: (repo, config, authority,
        {"physical_account_ids": ["1001"], "environment": "REAL"}))
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: {})
    monkeypatch.setattr(operations, "read_attribution_combo_evidence", lambda *a, **k: {"complete": True, "exposures": []})
    target = old_target if decision == "keep" else next(value for value in fact["candidate_ids"] if value != old_target)
    action = "ordinary" if decision == "ordinary" else "wheel " + target
    request = BotInboundRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room", config_key="hk")
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    before = repo.list_trade_events()
    result = operations.handle_attribution_operation(
        parse_bot_control_command(f"/attribute lx {fact['execution_key']} {action}"), request, command_id="preview", store=store)
    relation = next(line for line in result["data"]["response_text"].splitlines() if line.startswith("归属｜"))
    assert f"Wheel 批次 {old_target.removeprefix('wheel:')}：1 张 → " in relation
    assert relation.endswith("普通单腿" if decision == "ordinary" else "Wheel 批次 " + target.removeprefix("wheel:"))
    assert repo.list_trade_events() == before


@pytest.mark.parametrize("change", ["wheel_off", "activation", "policy", "combo_mode", "capacity", "quote", "cash_ttl", "unrelated", "after_commit"])
def test_control_rechecks_business_admission_with_same_authority(tmp_path, monkeypatch, change):
    from copy import deepcopy
    from test_attribution_conflict_decision import _scope, _view, _call, _args, _conflict, _statuses
    from src.application.trades import attribution
    repo, config = _scope(tmp_path, monkeypatch)
    config["portfolio"] = {"futu": {"host": "initial-capacity-host", "port": 11111}}
    fact = _call(_view(repo, config))
    old_target = fact["candidate_ids"][0]
    attribution.apply_trade_attribution(repo, **_args(config, fact, old_target, "initial"))
    conflict = _conflict(repo, _view(repo, config))
    fact = _call(_view(repo, config))
    target = next(value for value in fact["candidate_ids"] if value != old_target)
    for key, value in {"OM_INBOUND_OPERATIONS_ENABLED": "1", "OM_INBOUND_TRADE_WRITE_ENABLED": "1",
                       "OM_INBOUND_ADMIN_OPEN_IDS": "wechat:user", "OM_INBOUND_OPERATION_HMAC_KEY": "isolated"}.items():
        monkeypatch.setenv(key, value)
    authority = {"config_path": str(tmp_path / "config.hk.json"), "runtime_root": str(tmp_path),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": "fixture"}
    active_config = [config]
    monkeypatch.setattr(operations, "attribution_runtime", lambda **_: (repo, active_config[0], authority.copy(),
        {"physical_account_ids": ["1001"], "environment": "REAL"}))
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: {})
    monkeypatch.setattr(operations, "read_attribution_combo_evidence", lambda *a, **k: {"complete": True, "exposures": []})
    request = BotInboundRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room", config_key="hk")
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    operations.handle_attribution_operation(parse_bot_control_command(f"/attribute lx {fact['execution_key']} wheel {target}"),
        request, command_id="in_preview", store=store)
    before = (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots())
    def change_config():
        updated = deepcopy(config)
        if change in {"wheel_off", "after_commit"}:
            updated["wheel"]["accounts"] = []
        elif change == "activation":
            updated["wheel"]["activation_by_account"]["lx"]["generation"] += 1
        elif change == "policy":
            updated["wheel"]["call"] = {"max_dte": 999}
        elif change == "combo_mode":
            updated["trade_intake"] = {"combo_reconciliation": {"accounts": {"lx": "observe"}}}
        elif change == "capacity":
            updated["account_settings"]["lx"]["futu"]["host"] = "changed-capacity-host"
        elif change == "cash_ttl":
            updated["runtime"] = {"portfolio_context_ttl_sec": 1}
        elif change == "quote":
            updated["symbols"] = [{"symbol": "3690.HK", "fetch": {"source": "opend", "host": "changed-quote-host"}}]
        else:
            updated["notifications"] = {"display_note": "irrelevant"}
            updated["wheel"]["accounts"].append("sy")
        active_config[0] = updated
    writer = attribution.write_trade_attribution_decision
    def changed(*args, **kwargs):
        result = writer(*args, **kwargs)
        if change != "after_commit":
            change_config()
        return result
    monkeypatch.setattr(attribution, "write_trade_attribution_decision", changed)
    result = operations.handle_attribution_operation(parse_bot_control_command("/confirm attribution in_preview"),
        request, command_id="confirm", store=store)
    if change in {"unrelated", "after_commit"}:
        assert result["data"]["status"] == "applied", result
        assert _statuses(repo)[conflict]["resolved"]
        change_config()
        monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **_: pytest.fail("retry must only read back"))
        retry = operations.handle_attribution_operation(parse_bot_control_command("/confirm attribution in_preview"),
            request, command_id="retry", store=store)
        assert retry["data"]["status"] == "applied", retry
    else:
        assert result["data"]["status"] == "failed", result
        assert "归属准入配置或策略已改变" in result["data"]["result"]["reason"]
        assert (repo.list_trade_events(), repo.list_wheel_events(account="lx"), repo.list_position_lots()) == before
        assert not _statuses(repo)[conflict]["resolved"]


@pytest.mark.parametrize("relationship", ["multiple_allocations", "combo"])
def test_preview_renders_complete_old_relationship(attribution_context, relationship):
    from src.application.bot.control.renderer import render_attribution_preview
    repo, store, request, preview, _ = attribution_context
    operations.handle_attribution_operation(preview, request, command_id="preview", store=store)
    operation = store.get("preview")
    fact = operation["preview"]["members"][0]
    fact["status"] = "linked"
    if relationship == "multiple_allocations":
        fact["wheel_call_allocations"] = [
            {"stock_lot_id": "old-stock-a", "wheel_branch_id": "old-branch-a", "contracts": 1},
            {"stock_lot_id": "old-stock-b", "wheel_branch_id": "old-branch-b", "contracts": 2}]
        expected = "Wheel 分支 old-branch-a：1 张（股票批次 old-stock-a）；Wheel 分支 old-branch-b：2 张（股票批次 old-stock-b）"
    else:
        fact["strategy_group_id"] = "old-combo-group"
        expected = "Combo Yield old-combo-group"
    assert f"归属｜{expected} → 普通单腿" in render_attribution_preview(operation)
    assert len(repo.list_trade_events()) == 1
