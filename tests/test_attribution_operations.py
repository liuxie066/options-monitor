from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from src.application.agent_tool_contracts import AgentToolError

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_execution import execution_identity_from_input
from src.application.assistant import attribution_operations as operations
from src.application.assistant.contracts import AssistantRequest, ControlCommand
from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.inbound_control import execute_explicit_control
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.permission_response import parse_permission_response
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
    monkeypatch.setattr(operations, "attribution_runtime", lambda **_: (repo, {}, authority.copy(), mapping.copy()))
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    request = AssistantRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room",
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
    retry = parse_assistant_command("/confirm attribution in_preview")
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
    request = AssistantRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room", config_key="us")
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
    request = AssistantRequest(text="归属", sender_id="user", channel="wechat", conversation_id="room", config_key="us")
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
    from src.application.assistant.capability_catalog import preview_operation_capabilities
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
    from src.application.assistant.inbound_service import handle_assistant_request
    from src.application.assistant.audit import InboundAuditStore
    repo, store, request, preview, _ = attribution_context
    audit = InboundAuditStore(store.path)
    result = handle_assistant_request(replace(request, message_id="preview", conversation_id=conversation,
        text=f"/attribute lx {preview.arguments['execution_key']} ordinary"), audit_store=audit, allowed_senders="wechat:user")
    assert result["ok"] is (conversation == "room"), result
    assert len(repo.list_trade_events()) == 1
    if conversation == "room":
        operation_id = result["data"]["command_id"]
        for index, invalid in enumerate([None, "", " ", " room "]):
            rejected = handle_assistant_request(replace(request, message_id=f"confirm-{index}", conversation_id=invalid,
                text=f"/confirm attribution {operation_id}"), audit_store=audit, allowed_senders="wechat:user")
            assert not rejected["ok"], rejected
        assert store.get(operation_id)["status"] == "previewed"
        applied = handle_assistant_request(replace(request, message_id="valid-confirm",
            text=f"/confirm attribution {operation_id}"), audit_store=audit, allowed_senders="wechat:user")
        assert applied["ok"] and len(repo.list_trade_events()) == 2
    else:
        assert store.get(result["data"]["command_id"]) is None


@pytest.mark.parametrize("failure", ["cancel", "persistence"])
def test_bot_handoff_is_discarded_after_terminal_failure(attribution_context, monkeypatch, example_config_path, tmp_path, failure):
    from src.application.bot.contracts import BotRequest, BotScope
    from src.application.bot.service import prepare_contract
    from src.application.bot.host_store import BotHostStore
    from src.application.assistant.capability_catalog import preview_operation_capabilities
    from src.application.assistant import inbound_service
    from src.application.assistant.audit import InboundAuditStore
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
    response = inbound_service.handle_assistant_request(replace(request, text="归属这笔订单", message_id="failed-bot"),
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
    from src.application.assistant import operation_store
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
    from src.application.assistant.inbound_service import handle_assistant_request
    from src.application.assistant.audit import InboundAuditStore
    repo, store, request, preview, _ = attribution_context
    audit = InboundAuditStore(store.path)
    def send(text, message):
        return handle_assistant_request(replace(request, text=text, message_id=message),
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
