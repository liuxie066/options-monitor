"""Public control recovery, with only temporary SQLite ledgers and injected failures."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from src.application.bot.control import manual_trade_operations as operations
from src.application.bot.control.operation_signature import hash_operation_payload
from src.application.bot.control.operation_store import InboundOperationStore
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from test_inbound_control import _enable_inbound_trade_write, _handle, _write_inbound_runtime_config


@pytest.fixture
def recovery_context(tmp_path, monkeypatch):
    _enable_inbound_trade_write(monkeypatch)
    cfg, ledger = _write_inbound_runtime_config(tmp_path)
    audit = tmp_path / "inbound.sqlite3"
    counter = 0

    def handle(text, **kwargs):
        nonlocal counter
        counter += 1
        return _handle(text, f"recovery-message-{counter}", config_path=str(cfg), audit_db=str(audit), **kwargs)

    preview = handle("/record-open sy NVDA short put strike 100 exp 2027-06-18 1张 premium 2.5 multiplier 100")
    assert preview["ok"], preview
    return handle, InboundOperationStore(audit), ledger, preview["data"]["operation_id"]


def _interrupt_after_commit(handle, store, operation_id, monkeypatch, *, after_apply=False):
    with monkeypatch.context() as patch:
        if after_apply:
            original = operations._apply_operation

            def interrupted(payload):
                original(payload)
                raise RuntimeError("lost after ledger commit")

            patch.setattr(operations, "_apply_operation", interrupted)
        else:
            def interrupted(*args, **kwargs):
                raise RuntimeError("status database unavailable")

            patch.setattr(InboundOperationStore, "mark_applied", interrupted)
        failed_receipt = handle(f"/confirm trade {operation_id}")
        assert not failed_receipt["ok"]
    return store.get(operation_id)


@pytest.mark.parametrize("after_apply", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
def test_public_confirmation_recovers_committed_trade_after_restart(recovery_context, monkeypatch, after_apply, timeout):
    handle, store, ledger, operation_id = recovery_context
    _interrupt_after_commit(handle, store, operation_id, monkeypatch, after_apply=after_apply)
    assert len(SQLiteOptionPositionsRepository(ledger).list_trade_events()) == 1
    restarted = InboundOperationStore(store.path)
    if timeout:
        restarted.reconcile_stale_operations(now=datetime.now(timezone.utc) + timedelta(days=1))
    recovered = handle(f"/confirm trade {operation_id}")
    assert recovered["ok"], recovered
    assert recovered["data"]["status"] == "applied"
    assert restarted.get(operation_id)["status"] == "applied"
    repeated = handle(f"/confirm trade {operation_id}")
    assert not repeated["ok"], repeated
    assert repeated["error"]["code"] == "INPUT_ERROR"
    assert restarted.get(operation_id)["status"] == "applied"
    assert len(SQLiteOptionPositionsRepository(ledger).list_trade_events()) == 1


def test_timeout_without_durable_identity_stays_unverified(recovery_context):
    handle, store, ledger, operation_id = recovery_context
    # A legacy confirmed operation can lack a stable ledger request identity.
    operation = store.get(operation_id)
    payload = operation["payload"]
    payload["arguments"].pop("request_id")
    store.update_preview(operation_id, payload=payload, payload_hash=hash_operation_payload(payload), preview=operation["preview"])
    assert store.mark_confirmed(operation_id)
    store.reconcile_stale_operations(now=datetime.now(timezone.utc) + timedelta(days=1))
    assert store.get(operation_id)["status"] == "confirmed"
    result = handle(f"/confirm trade {operation_id}")
    assert not result["ok"]
    assert result["error"]["details"]["reason"] == "ledger_result_unverified"
    assert "待核实" in result["error"]["message"]
    assert SQLiteOptionPositionsRepository(ledger).list_trade_events() == []


def test_confirmed_without_commit_never_replays_write(recovery_context, monkeypatch):
    handle, store, ledger, operation_id = recovery_context
    assert store.mark_confirmed(operation_id)
    monkeypatch.setattr(operations, "_apply_operation", lambda _: pytest.fail("recovery must not apply"))
    result = handle(f"/confirm trade {operation_id}")
    assert not result["ok"]
    assert result["error"]["details"]["reason"] == "ledger_result_unverified"
    assert store.get(operation_id)["status"] == "confirmed"
    assert SQLiteOptionPositionsRepository(ledger).list_trade_events() == []


@pytest.mark.parametrize("tamper", ["payload", "signature", "sender", "conversation", "policy"])
def test_recovery_preserves_authorization_and_integrity_gates(recovery_context, monkeypatch, tamper):
    handle, store, ledger, operation_id = recovery_context
    _interrupt_after_commit(handle, store, operation_id, monkeypatch)
    kwargs = {}
    if tamper in {"payload", "signature"}:
        with sqlite3.connect(store.path) as conn:
            if tamper == "payload":
                payload = store.get(operation_id)["payload"]
                payload["arguments"]["contracts"] = 2
                conn.execute("UPDATE inbound_pending_operations SET payload_json = ? WHERE operation_id = ?", (json.dumps(payload), operation_id))
            else:
                conn.execute("UPDATE inbound_pending_operations SET signature = 'invalid' WHERE operation_id = ?", (operation_id,))
    elif tamper == "sender":
        kwargs = {"sender_id": "other", "allowed_senders": "feishu:other"}
        monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1,feishu:other")
    elif tamper == "conversation":
        kwargs = {"conversation_id": "another-room"}
    else:
        monkeypatch.setenv("OM_INBOUND_TRADE_WRITE_ENABLED", "0")
    result = handle(f"/confirm trade {operation_id}", **kwargs)
    assert not result["ok"], result
    assert store.get(operation_id)["status"] != "applied"
    assert len(SQLiteOptionPositionsRepository(ledger).list_trade_events()) == 1


def test_legacy_failed_receipt_can_recover_a_committed_operation(recovery_context, monkeypatch):
    handle, store, ledger, operation_id = recovery_context
    _interrupt_after_commit(handle, store, operation_id, monkeypatch)
    store.mark_failed(operation_id, result={"reason": "operation_stale_after_confirmation"})
    result = handle(f"/confirm trade {operation_id}")
    assert result["ok"], result
    assert store.get(operation_id)["status"] == "applied"
    assert len(SQLiteOptionPositionsRepository(ledger).list_trade_events()) == 1


@pytest.mark.parametrize("evidence_change", ["identity", "price", "unavailable"])
def test_recovery_requires_matching_durable_proof(recovery_context, monkeypatch, evidence_change):
    handle, store, ledger, operation_id = recovery_context
    _interrupt_after_commit(handle, store, operation_id, monkeypatch)
    events = SQLiteOptionPositionsRepository(ledger).list_trade_events()
    if evidence_change == "identity":
        events[0]["raw_payload"]["manual_request_id"] = "another-operation"
    elif evidence_change == "price":
        events[0]["price"] = "99"
    else:
        def unavailable(*args, **kwargs):
            raise OSError("isolated ledger unavailable")
        monkeypatch.setattr(operations, "trade_event_log", unavailable)
    if evidence_change != "unavailable":
        monkeypatch.setattr(operations, "trade_event_log", lambda _: events)
    result = handle(f"/confirm trade {operation_id}")
    assert not result["ok"], result
    assert result["error"]["details"]["reason"] == "ledger_result_unverified"
    assert store.get(operation_id)["status"] == "confirmed"


def test_recovery_applied_mark_uses_payload_and_status_cas(recovery_context, monkeypatch):
    handle, store, ledger, operation_id = recovery_context
    _interrupt_after_commit(handle, store, operation_id, monkeypatch)
    read_result = operations._read_committed_operation_result

    def concurrent_change(operation):
        result = read_result(operation)
        assert store.mark_cancelled(operation_id, result={"status": "cancelled"}, expected_statuses=("confirmed",))
        return result

    monkeypatch.setattr(operations, "_read_committed_operation_result", concurrent_change)
    result = handle(f"/confirm trade {operation_id}")
    assert not result["ok"], result
    assert store.get(operation_id)["status"] == "cancelled"
    assert len(SQLiteOptionPositionsRepository(ledger).list_trade_events()) == 1


@pytest.mark.parametrize("batch,scenario", [(False, "normal"), (True, "normal"), (True, "expired_child"), (True, "crowded")])
def test_expiry_persists_operation_identity_and_recovers_public_confirmation(recovery_context, monkeypatch, batch, scenario):
    from src.application.positions.workflows import execute_manual_open

    handle, store, ledger, _operation_id = recovery_context
    repo = SQLiteOptionPositionsRepository(ledger)
    execute_manual_open(
        repo, broker="富途", account="sy", symbol="NVDA", option_type="put", side="short",
        contracts=1, currency="USD", strike=100, multiplier=100, expiration_ymd="2026-07-10",
        premium_per_share=2.5, underlying_share_locked=None, note=None, dry_run=False,
        opened_at_ms=int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp() * 1000), request_id="expiry-seed",
    )
    draft = {"arguments": {"broker": "富途", "account": "sy", "symbol": "NVDA", "option_type": "put",
                           "position_side": "short", "contracts_to_close": 1, "strike": 100,
                           "expiration_ymd": "2026-07-10", "event_time_ms": int(datetime(2026, 7, 11, tzinfo=timezone.utc).timestamp() * 1000)},
             "diagnostics": {}}
    drafts = [draft]
    if batch:
        execute_manual_open(
            repo, broker="富途", account="sy", symbol="AMD", option_type="put", side="short",
            contracts=1, currency="USD", strike=100, multiplier=100, expiration_ymd="2026-07-10",
            premium_per_share=2.5, underlying_share_locked=None, note=None, dry_run=False,
            opened_at_ms=int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp() * 1000), request_id="expiry-seed-amd",
        )
        drafts.append({"arguments": {**draft["arguments"], "symbol": "AMD"}, "diagnostics": {}})
    monkeypatch.setattr(operations, "build_manual_expiry_drafts", lambda **_: drafts)
    preview = handle("/record-expiry isolated expiry notice")
    assert preview["ok"], preview
    operation_id = preview["data"].get("operation_id") or preview["data"]["command_id"]
    _interrupt_after_commit(handle, store, operation_id, monkeypatch)
    child_ids = preview["data"].get("operation_ids") or [operation_id]
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE inbound_pending_operations SET confirmed_at = ? WHERE status = 'confirmed'", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    store.reconcile_stale_operations()
    if scenario == "expired_child":
        from src.application.bot.control.operation_signature import sign_operation_fields
        child = store.get(child_ids[1])
        child["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        signature = sign_operation_fields(operation=child)
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE inbound_pending_operations SET expires_at = ?, signature = ? WHERE operation_id = ?", (child["expires_at"], signature["signature"], child_ids[1]))
    elif scenario == "crowded":
        original = store.get(child_ids[0])
        for index in range(105):
            store.save_preview(operation_id=f"in_unrelated_{index}", command_id=f"in_unrelated_{index}", channel=original["channel"], sender_id=original["sender_id"], conversation_id=original["conversation_id"], operation_type="manual_expiry", payload=original["payload"], payload_hash=original["payload_hash"], preview=original["preview"], ttl_seconds=600)
    result = handle(f"/confirm trade {operation_id}")
    if scenario == "expired_child":
        assert not result["ok"], result
        assert store.get(child_ids[0])["status"] == "applied"
        assert store.get(child_ids[1])["status"] == "expired"
        assert len([event for event in repo.list_trade_events() if event["event_type"] == "expire_close"]) == 1
        return
    assert result["ok"], result
    assert result["data"]["status"] == "applied"
    assert all(store.get(child)["status"] == "applied" for child in child_ids)
    if batch:
        assert result["data"]["applied_count"] == len(child_ids)
        assert set(result["data"]["operation_ids"]) == set(child_ids)
    again = handle(f"/confirm trade {operation_id}")
    if batch:
        assert again["ok"], again
        assert again["data"]["applied_count"] == 0
        assert again["data"]["already_applied_count"] == len(child_ids)
    else:
        assert not again["ok"]
        assert again["error"]["code"] == "INPUT_ERROR"
    expiry_events = [event for event in repo.list_trade_events() if event["event_type"] == "expire_close"]
    assert len(expiry_events) == len(drafts)
    assert all(event["raw_payload"]["evidence_ids"] for event in expiry_events)


@pytest.mark.parametrize("already_applied", [False, True])
def test_voided_operation_is_not_reported_applied(recovery_context, monkeypatch, already_applied):
    from src.application.ledger.api import record_trade_event_void

    handle, store, ledger, operation_id = recovery_context
    _interrupt_after_commit(handle, store, operation_id, monkeypatch)
    if already_applied:
        assert handle(f"/confirm trade {operation_id}")["ok"]
    repo = SQLiteOptionPositionsRepository(ledger)
    event_id = repo.list_trade_events()[0]["event_id"]
    record_trade_event_void(repo, event_id=event_id, reason="isolated recovery regression")
    result = handle(f"/confirm trade {operation_id}")
    assert not result["ok"], result
    if not already_applied:
        assert result["error"]["details"]["reason"] == "ledger_result_unverified"
    assert len(repo.list_trade_events()) == 2


def test_prewrite_preview_error_keeps_original_error_and_failed_status(recovery_context, monkeypatch):
    from src.application.agent_tool_contracts import AgentToolError

    handle, store, ledger, operation_id = recovery_context

    def invalid_preview(_):
        raise AgentToolError(code="INPUT_ERROR", message="prewrite validation failed", hint="fix input")

    monkeypatch.setattr(operations, "_preview_operation", invalid_preview)
    result = handle(f"/confirm trade {operation_id}")
    assert not result["ok"]
    assert result["error"]["code"] == "INPUT_ERROR"
    assert result["error"]["message"] == "prewrite validation failed"
    assert result["error"]["hint"] == "fix input"
    assert store.get(operation_id)["status"] == "failed"
    assert SQLiteOptionPositionsRepository(ledger).list_trade_events() == []


@pytest.mark.parametrize("unavailable", [False, True])
def test_recovery_closes_repository_on_success_and_error(recovery_context, monkeypatch, unavailable):
    handle, store, ledger, operation_id = recovery_context
    _interrupt_after_commit(handle, store, operation_id, monkeypatch)
    original_open = operations._open_repo_for_payload
    closed = []

    def tracked_open(payload, **kwargs):
        data_config, repo, cfg = original_open(payload, **kwargs)
        repo.close = lambda: closed.append(True)
        return data_config, repo, cfg

    monkeypatch.setattr(operations, "_open_repo_for_payload", tracked_open)
    if unavailable:
        def error(_):
            raise OSError("isolated query failure")
        monkeypatch.setattr(operations, "trade_event_log", error)
    result = handle(f"/confirm trade {operation_id}")
    assert result["ok"] is not unavailable
    assert closed == [True]


def test_close_without_stable_operation_marker_remains_unresolved(recovery_context, monkeypatch):
    handle, store, ledger, operation_id = recovery_context
    assert handle(f"/confirm trade {operation_id}")["ok"]
    preview = handle("/record-close sy NVDA short put strike 100 exp 2027-06-18 1张 close 1.0")
    assert preview["ok"], preview
    close_id = preview["data"]["operation_id"]
    _interrupt_after_commit(handle, store, close_id, monkeypatch)
    store.reconcile_stale_operations(now=datetime.now(timezone.utc) + timedelta(days=1))
    result = handle(f"/confirm trade {close_id}")
    assert not result["ok"]
    assert result["error"]["details"]["reason"] == "ledger_result_unverified"
    assert store.get(close_id)["status"] == "confirmed"
    assert len(SQLiteOptionPositionsRepository(ledger).list_trade_events()) == 2


def test_assignment_recovery_uses_canonical_request_content(recovery_context, monkeypatch):
    from src.application.bot.control.contracts import BotInboundRequest, ControlCommand
    from src.application.bot.control.inbound_control import execute_explicit_control

    handle, store, ledger, operation_id = recovery_context
    assert handle(f"/confirm trade {operation_id}")["ok"]
    opened = store.get(operation_id)
    draft_args = {"broker": "富途", "account": "sy", "symbol": "NVDA", "option_type": "put",
                  "position_side": "short", "contracts_to_close": 1, "strike": 100,
                  "expiration_ymd": "2027-06-18", "stock_side": "buy", "stock_qty": 100, "stock_price": 100}
    monkeypatch.setattr(operations, "build_manual_trade_draft", lambda *a, **k: {"arguments": draft_args, "diagnostics": {}})
    request = BotInboundRequest(text="isolated assignment", channel="feishu", sender_id="ou_1",
                                conversation_id=opened["conversation_id"], config_path=opened["payload"]["config"]["config_path"])
    preview = execute_explicit_control(ControlCommand("manual_assignment", {}), request=request, command_id="in_assignment_recovery", operation_store=store)
    assert preview.ok, preview
    _interrupt_after_commit(handle, store, "in_assignment_recovery", monkeypatch)
    store.reconcile_stale_operations(now=datetime.now(timezone.utc) + timedelta(days=1))
    result = handle("/confirm trade in_assignment_recovery")
    assert result["ok"], result
    assert store.get("in_assignment_recovery")["status"] == "applied"
    repeated = handle("/confirm trade in_assignment_recovery")
    assert not repeated["ok"]
    assert repeated["error"]["code"] == "INPUT_ERROR"
    assert len(SQLiteOptionPositionsRepository(ledger).list_trade_events()) == 2
