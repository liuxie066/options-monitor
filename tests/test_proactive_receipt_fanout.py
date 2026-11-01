"""Producer integrations use only isolated state and mock notification transports."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from domain.domain.multi_tick import notification_route_identity_hash

from src.application import notification_fanout as fanout
from src.application import notification_fanout_delivery as delivery
from src.application import notification_delivery_adapter as adapter
from src.application import system_alerts
from src.application.ledger import notification_outbox as outbox
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions import maintenance_receipt as maintenance
from src.application.trades import inbox, lifecycle_outbox, receipt


@pytest.fixture
def controls(monkeypatch):
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "fixture-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "legacy-user")
    clock = [2_000_000_000.0]
    monkeypatch.setattr(fanout.time, "time", lambda: clock[0])
    return {"notifications": {"enabled": True, "routes": [
        {"id": name, "provider": "feishu_app", "target": name, "accounts": ["lx"]}
        for name in ("A", "B", "C")]}, "channels": {
            "feishu": {"enabled": True, "notify_enabled": True}}}, clock


def _mock_transport():
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        target = kwargs["target"]
        if target == "B" and sum(call["target"] == "B" for call in calls) == 1:
            return {"explicit_pre_acceptance_failure": True, "delivery_confirmed": False}
        if target == "C":
            return {"delivery_confirmed": False, "ambiguous_send": True}
        return {"delivery_confirmed": True, "message_id": "fixture-" + target,
                "attribution_route_key": notification_route_identity_hash(provider="feishu_app",
                    channel="feishu_app", sender="fixture-app", recipient=target)}

    return calls, send


def _normalizer(send_result):
    return send_result


def test_frozen_children_partial_retry_controls_and_new_routes(tmp_path, controls):
    config, clock = controls
    calls, send = _mock_transport()
    kwargs = dict(base=tmp_path, config=config, account="lx", logical_key="fixture", message="original",
                  send_fn=send, normalize_fn=_normalizer)
    first = delivery.send_notification_fanout(**kwargs)
    assert first["status"] == "partial" and first["has_retryable_children"]
    assert [child["status"] for child in first["children"]] == ["confirmed", "explicit_failed", "unknown"]
    config["notifications"]["routes"].append(
        {"id": "new", "provider": "feishu_app", "target": "new", "accounts": ["lx"]})
    clock[0] += 61
    config["channels"]["feishu"]["notify_enabled"] = False
    paused = delivery.send_notification_fanout(**kwargs)
    assert [child["attempt_count"] for child in paused["children"]] == [1, 1, 1]
    config["channels"]["feishu"]["notify_enabled"] = True
    second = delivery.send_notification_fanout(**{**kwargs, "message": "changed renderer"})
    assert [call["target"] for call in calls] == ["A", "B", "C", "B"]
    assert calls[-1]["message"] == "original"
    assert calls[1]["idempotency_key"] == calls[-1]["idempotency_key"]
    assert second["status"] == "partial" and not second["has_retryable_children"]


@pytest.mark.parametrize("change", ["remove", "rotate", "emergency"])
def test_current_route_changes_do_not_consume_pending_children(tmp_path, controls, change):
    config, clock = controls
    calls, send = _mock_transport()
    kwargs = dict(base=tmp_path, config=config, account="lx", logical_key="fixture", message="frozen",
                  send_fn=send, normalize_fn=_normalizer)
    delivery.send_notification_fanout(**kwargs)
    clock[0] += 61
    if change == "remove":
        config["notifications"]["routes"] = [item for item in config["notifications"]["routes"] if item["id"] != "B"]
    elif change == "rotate":
        config["notifications"]["routes"][1]["target"] = "rotated"
    else:
        config["notifications"]["enabled"] = False
    state = delivery.send_notification_fanout(**kwargs)
    assert len(calls) == 3
    assert state["children"][1]["attempt_count"] == 1


def test_missing_package_is_explicit_failure_not_unknown_or_success(tmp_path, controls):
    from src.application.agent_tool_contracts import AgentToolError
    config, _ = controls
    def unavailable(**kwargs):
        raise AgentToolError(code="CHANNEL_PLUGIN_NOT_INSTALLED", message="fixture")
    state = delivery.send_notification_fanout(base=tmp_path, config=config, account="lx",
        logical_key="missing-package", message="frozen", send_fn=unavailable, normalize_fn=_normalizer)
    assert state["status"] == "explicit_failed" and state["has_retryable_children"]
    assert all(child["provider_receipt"]["local_error_code"] == "CHANNEL_PLUGIN_NOT_INSTALLED" for child in state["children"])


def _inbox_receipt(tmp_path, config):
    path = tmp_path / "inbox.sqlite3"
    key = inbox.enqueue_trade_payload(path, payload={"deal_id": "one", "futu_account_id": "123", "account": "lx"},
        source="push", broker_deal_key="futu:lx:123:one")
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET receipt_recovery_allowed=1 WHERE inbox_id=?", (key,))
    claim = inbox.claim_trade_payload(path, inbox_id=key)
    result = inbox.save_trade_payload_result(path, claim=claim,
        result={"account": "lx", "status": "failed", "reason": "fixture_failure",
                "diagnostics": {"retryable": False}, "_receipt_payload": {"account": "lx"}})
    inbox.settle_trade_payload_result(path, inbox_id=key, claim=claim, result=result)
    return path, key, dict(base=tmp_path, config=config, receipt_config={}, apply_changes=True, state=None,
        deal=SimpleNamespace(deal_id="one", internal_account="lx"), result=result,
        payload={"account": "lx"}, inbox_path=path, inbox_id=key)


def test_inbox_crash_after_partial_send_resumes_delivery_without_economic_claim(tmp_path, controls, monkeypatch):
    config, clock = controls
    path, key, kwargs = _inbox_receipt(tmp_path, config)
    calls, send = _mock_transport()
    finish = inbox.finish_trade_receipt_attempt
    def crash(*args, **kwargs):
        raise RuntimeError("fixture crash before parent projection")
    monkeypatch.setattr(inbox, "finish_trade_receipt_attempt", crash)
    with pytest.raises(RuntimeError, match="fixture crash"):
        receipt.send_trade_intake_receipt(**kwargs, send_fn=send, normalize_fn=_normalizer)
    before = inbox.read_trade_payload(path, inbox_id=key)
    assert before["receipt"]["fanout_id"] and before["receipt"]["status"] == "unknown"
    clock[0] += 61
    assert [row["inbox_id"] for row in inbox.list_trade_receipt_recovery_rows(path, account_ids=["123"])] == [key]
    monkeypatch.setattr(inbox, "finish_trade_receipt_attempt", finish)
    result = receipt.send_trade_intake_receipt(**kwargs, send_fn=send, normalize_fn=_normalizer)
    assert [call["target"] for call in calls] == ["A", "B", "C", "B"]
    after = inbox.read_trade_payload(path, inbox_id=key)
    assert after["attempt_count"] == before["attempt_count"]
    assert after["result"] == before["result"]
    assert result["status"] == "partial" and not result["has_retryable_children"]
    assert inbox.list_trade_receipt_recovery_rows(path, account_ids=["123"]) == []


def _enqueue(repo, account, suffix):
    intent = outbox.build_notification_intent(case_id="case-" + suffix, transition_type="needs_review",
        resolution_revision=1, transition_key="review-" + suffix, state_fingerprint=suffix,
        payload={"account": account, "symbol": "NVDA", "transition_type": "needs_review", "market": "US"})
    outbox.enqueue_notification_intent(repo, intent)
    return intent


def test_lifecycle_crash_recovers_only_failed_child_and_keeps_account_batches_separate(tmp_path, controls, monkeypatch):
    config, clock = controls
    config["notifications"]["routes"].append({"id": "sy", "provider": "feishu_app", "target": "sy", "accounts": ["sy"]})
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    _enqueue(repo, "lx", "one")
    _enqueue(repo, "sy", "two")
    clock[0] += 11
    route = receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=tmp_path)
    calls, send = _mock_transport()
    def dispatch():
        return lifecycle_outbox.dispatch_notification_batch_once(repo, route=route, now_ms=int(clock[0] * 1000),
            allowed_accounts=["lx", "sy"], send_fn=lambda payload: receipt.send_trade_lifecycle_outbox_payload(
                base=tmp_path, config=config, receipt_config={}, payload=payload, send_fn=send, normalize_fn=_normalizer))
    complete = lifecycle_outbox.complete_notification_batch_attempt
    def crash(*args, **kwargs):
        raise KeyboardInterrupt("fixture crash")
    monkeypatch.setattr(lifecycle_outbox, "complete_notification_batch_attempt", crash)
    with pytest.raises(KeyboardInterrupt):
        dispatch()
    batches = repo.list_trade_lifecycle_notification_batches()
    assert len(batches) == 1
    assert {member["payload"]["account"] for member in batches[0]["payload"]["members"]} == {"lx"}
    assert batches[0]["provider_receipt"]["fanout_id"]
    clock[0] += 301
    monkeypatch.setattr(lifecycle_outbox, "complete_notification_batch_attempt", complete)
    result = dispatch()
    assert result["status"] == "partial"
    assert not result["batch"]["provider_receipt"]["has_retryable_children"]
    assert [call["target"] for call in calls] == ["A", "B", "C", "B"]
    clock[0] += 61
    assert dispatch()["status"] == "confirmed"
    assert calls[-1]["target"] == "sy"
    assert "lx" not in calls[-1]["message"]


def test_system_alert_fanout_never_falls_back_and_resumes_disabled_obligation(tmp_path, controls, monkeypatch):
    config, clock = controls
    calls, send = _mock_transport()
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
        lambda _: SimpleNamespace(send_fn=send, normalize_fn=_normalizer))
    fields = dict(base=tmp_path, config=config, unit="fixture", market="us", account="lx",
        failure_code="FIXTURE", stage="fixture", run_id="one", rc=1, first_error_at="fixture", opend_login_state="unknown")
    system_alerts.report_system_failure(**fields)
    clock[0] += 601
    state_path = tmp_path / "output_shared/state/system_alerts.json"
    state = json.loads(state_path.read_text())
    next(iter(state.values()))["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    state_path.write_text(json.dumps(state))
    system_alerts.report_system_failure(**fields)
    assert [call["target"] for call in calls] == ["A", "B", "C", "B"]
    assert system_alerts._send(tmp_path, config, "other", "other", account="sy")["attempted"] is False


def test_feishu_explicit_route_honors_target_legacy_keeps_env(tmp_path, controls, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "send_post_message", lambda **kwargs: calls.append(kwargs) or {"code": 0, "data": {"message_id": "fixture"}})
    for notifications in ({}, {"_explicit_route_target": True}):
        adapter.send_feishu_app_message(base=tmp_path, channel="feishu_app", target="route-user", message="fixture", notifications=notifications)
    assert [call["open_id"] for call in calls] == ["legacy-user", "route-user"]


def test_maintenance_crashed_parent_continues_on_later_noop(tmp_path, controls, monkeypatch):
    config, clock = controls
    calls, send = _mock_transport()
    selector = lambda _: SimpleNamespace(send_fn=send, normalize_fn=_normalizer)
    monkeypatch.setattr(maintenance, "select_notification_delivery_adapter", selector)
    result = {"account": "lx", "broker": "futu", "mode": "apply", "applied_closed": 1,
        "candidates_should_close": 1, "as_of_utc": "2026-11-01T00:00:00+00:00", "applied": [{"record_id": "lot-one"}]}
    first = maintenance.send_auto_close_receipt(base=tmp_path, config=config, receipt_config={}, dry_run=False,
        result=result, send_fn=send, normalize_fn=_normalizer)
    assert first["fanout_id"]
    clock[0] += 61
    noop = {**result, "applied_closed": 0, "candidates_should_close": 0, "applied": []}
    resumed = maintenance.safe_send_auto_close_receipt(base=tmp_path, config=config, dry_run=False, result=noop)
    assert [call["target"] for call in calls] == ["A", "B", "C", "B"]
    assert resumed["delivery_continuations"][0]["fanout_id"] == first["fanout_id"]
    assert result["applied_closed"] == 1 and noop["applied_closed"] == 0


@pytest.mark.parametrize("notify_dry_run", [None, False, True])
def test_public_auto_close_preview_preserves_old_fanout_then_apply_resumes_failed_only(
        tmp_path, controls, monkeypatch, notify_dry_run):
    from functools import partial
    import socket

    from src.application.positions import auto_close

    def forbid_network(*args, **kwargs):
        raise AssertionError("auto-close regression must use only mock transports")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    config, clock = controls
    config.update(accounts=["lx"], portfolio={"data_config": str(tmp_path / "data.json"), "broker": "futu"})
    receipt_config = {} if notify_dry_run is None else {"notify_dry_run": notify_dry_run}
    config["option_positions"] = {"auto_close": {"enabled": True, "receipt": receipt_config}}
    config_path = tmp_path / "config.us.json"
    config_path.write_text(json.dumps(config))
    ledger_path = tmp_path / "output_shared/state/option_positions.sqlite3"
    (tmp_path / "data.json").write_text(json.dumps({"option_positions": {"sqlite_path": str(ledger_path)}}))
    repo = SQLiteOptionPositionsRepository(ledger_path)
    events_before = repo.list_trade_events()
    monkeypatch.setattr(auto_close, "load_config", lambda **_: config)
    calls, send = _mock_transport()
    selector_calls = []

    def select(provider):
        selector_calls.append(provider)
        return SimpleNamespace(send_fn=send, normalize_fn=_normalizer)

    monkeypatch.setattr(maintenance, "select_notification_delivery_adapter", select)
    monkeypatch.setattr(maintenance, "send_auto_close_receipt",
        partial(maintenance.send_auto_close_receipt, adapter_selector=select))
    old_result = {"account": "lx", "broker": "futu", "mode": "apply", "applied_closed": 1,
        "candidates_should_close": 1, "as_of_utc": "2026-11-01T00:00:00+00:00",
        "applied": [{"record_id": "already-closed"}]}
    first = maintenance.safe_send_auto_close_receipt(base=tmp_path, config=config, dry_run=False, result=old_result)
    assert [child["status"] for child in first["children"]] == ["confirmed", "explicit_failed", "unknown"]
    fanout_path, = (tmp_path / "output_shared/state/notification_fanout").rglob("*.json")
    receipt_path = maintenance._auto_close_receipt_state_path(base=tmp_path, account="lx")
    frozen_fanout, frozen_receipt = fanout_path.read_bytes(), receipt_path.read_bytes()
    clock[0] += 61
    selector_calls.clear()
    args = ["--runtime-root", str(tmp_path), "--config", str(config_path), "--accounts", "lx", "--quiet"]

    assert auto_close.main(args) == 0

    output_path = tmp_path / "output_shared/state/auto_close_expired.json"
    preview = json.loads(output_path.read_text())
    assert preview["status"] == "dry_run"
    assert preview["account_results"][0]["result"]["receipt"]["status"] == "skipped"
    assert not selector_calls
    assert [call["target"] for call in calls] == ["A", "B", "C"]
    assert fanout_path.read_bytes() == frozen_fanout
    assert receipt_path.read_bytes() == frozen_receipt
    assert repo.list_trade_events() == events_before

    assert auto_close.main([*args, "--apply", "--yes"]) == 0

    applied = json.loads(output_path.read_text())
    assert applied["status"] == "noop" and applied["summary"]["applied_closed"] == 0
    assert repo.list_trade_events() == events_before
    assert [call["target"] for call in calls] == ["A", "B", "C", "B"]
    resumed = fanout.read_notification_fanout(base=tmp_path, fanout_id=first["fanout_id"])
    assert [child["attempt_count"] for child in resumed["children"]] == [1, 2, 1]
    assert [child["spec"] for child in resumed["children"]] == [child["spec"] for child in first["children"]]


@pytest.mark.parametrize("family", ["inbox", "maintenance", "lifecycle", "alerts"])
def test_explicit_channels_without_routes_all_off_never_resolves_credentials(tmp_path, controls, monkeypatch, family):
    from src.application import notification_delivery_route as routing
    config, _ = controls
    config["notifications"] = {"enabled": True, "provider": "feishu_app", "target": "legacy-target"}
    config["channels"]["feishu"]["notify_enabled"] = False
    monkeypatch.setattr(routing, "resolve_feishu_bot_send_target", lambda **_: pytest.fail("disabled credentials"))
    if family == "inbox":
        path, key, kwargs = _inbox_receipt(tmp_path, config)
        result = receipt.send_trade_intake_receipt(**kwargs)
        assert inbox.read_trade_payload(path, inbox_id=key)["receipt"]["status"] == "failed"
    elif family == "maintenance":
        result = maintenance.safe_send_auto_close_receipt(base=tmp_path, config=config, dry_run=False,
            result={"account": "lx", "mode": "apply", "applied_closed": 1})
    elif family == "lifecycle":
        result = receipt.send_trade_lifecycle_outbox_payload(base=tmp_path, config=config, receipt_config={},
            payload={"account": "lx", "transition_type": "needs_review"})
    else:
        result = system_alerts._send(tmp_path, config, "fixture", "disabled", account="lx")
    assert not result["delivery_confirmed"] and result["has_retryable_children"]
    assert result.get("attempted") is False
    assert fanout.list_notification_fanouts(base=tmp_path, account="lx") == []


def test_gate_errors_do_not_consume_attempts(tmp_path, controls, monkeypatch):
    from src.application import notification_delivery_route as routing
    config, _ = controls
    def invalid(**kwargs):
        raise ValueError("fixture configuration incomplete")
    monkeypatch.setattr(routing, "notification_delivery_route_is_current", invalid)
    result = delivery.send_notification_fanout(base=tmp_path, config=config, account="lx",
        logical_key="unproven", message="frozen", send_fn=lambda **_: pytest.fail("gate"), normalize_fn=_normalizer)
    assert all(child["attempt_count"] == 0 for child in result["children"])


def test_legacy_target_under_explicit_channels_and_accepted_child_never_retries(tmp_path, controls):
    config, clock = controls
    config["notifications"] = {"enabled": True, "provider": "feishu_app", "target": "ignored-legacy-target"}
    calls = []
    def send(**kwargs):
        calls.append(kwargs)
        return {"command_ok": True, "delivery_confirmed": False}
    kwargs = dict(base=tmp_path, config=config, account="lx", logical_key="legacy-explicit", message="frozen",
                  send_fn=send, normalize_fn=_normalizer)
    first = delivery.send_notification_fanout(**kwargs)
    clock[0] += 1000
    second = delivery.send_notification_fanout(**kwargs)
    assert len(calls) == 1 and calls[0]["target"] == "legacy-user"
    assert first["children"][0]["status"] == "accepted"
    assert not second["has_retryable_children"]


def test_combo_fanout_uses_account_binding_and_never_rewrites_confirmation_on_retry(tmp_path, controls, monkeypatch):
    from test_trade_attribution_combo_boundary import _scope
    from test_combo_pair_success_receipt import _confirm
    repo, config, pair, observation, evidence, now = _scope(tmp_path, monkeypatch)
    route_config, _ = controls
    config.update(deepcopy(route_config))
    route = receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=tmp_path, account="lx")
    case = repo, config, pair, observation, evidence, now, route
    _confirm(case, apply_changes=True)
    events = deepcopy(repo.list_trade_events())
    calls, send = _mock_transport()
    def dispatch(current):
        current_route = receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=tmp_path)
        return lifecycle_outbox.dispatch_notification_batch_once(repo, route=current_route, now_ms=current,
            allowed_accounts=["lx"], send_fn=lambda payload: receipt.send_trade_lifecycle_outbox_payload(
                base=tmp_path, config=config, receipt_config={}, payload=payload, send_fn=send, normalize_fn=_normalizer))
    row, = repo.list_trade_lifecycle_notifications()
    ready = row["created_at_ms"] + lifecycle_outbox.QUIET_WINDOW_MS
    first = dispatch(ready)
    assert first["status"] == "partial"
    config["notifications"]["routes"].append({"id": "sy", "accounts": ["sy"], "provider": "feishu_app", "target": "sy"})
    assert receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=tmp_path, account="lx")["attribution_route_key"] == route["attribution_route_key"]
    monkeypatch.setattr(fanout.time, "time", lambda: (ready + 61_000) / 1000)
    second = dispatch(ready + 61_000)
    assert second["status"] == "partial" and not second["batch"]["provider_receipt"]["has_retryable_children"]
    assert [call["target"] for call in calls] == ["A", "B", "C", "B"]
    assert repo.list_trade_events() == events


def test_initial_all_off_maintenance_and_recovery_obligations_resume_when_enabled(tmp_path, controls, monkeypatch):
    config, clock = controls
    config["notifications"] = {"enabled": True, "provider": "feishu_app", "target": "legacy-target"}
    config["channels"]["feishu"]["notify_enabled"] = False
    calls, send = _mock_transport()
    selector = lambda _: SimpleNamespace(send_fn=send, normalize_fn=_normalizer)
    monkeypatch.setattr(maintenance, "select_notification_delivery_adapter", selector)
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", selector)
    result = {"account": "lx", "mode": "apply", "applied_closed": 1, "as_of_utc": "2026-11-01T00:00:00+00:00"}
    first = maintenance.safe_send_auto_close_receipt(base=tmp_path, config=config, dry_run=False, result=result)
    assert first["status"] == "pending" and not calls
    fields = dict(base=tmp_path, config=config, unit="fixture", market="us", account="lx", failure_code="fixture", stage="fixture")
    assert system_alerts.report_system_recovery(**fields, allow_without_incident=True) == "suppressed"
    config["channels"]["feishu"]["notify_enabled"] = True
    clock[0] += 61
    second = maintenance.safe_send_auto_close_receipt(base=tmp_path, config=config, dry_run=False,
        result={**result, "applied_closed": 0})
    assert second["delivery_continuations"][0]["delivery_confirmed"]
    assert system_alerts.report_system_recovery(**fields) == "confirmed"
    assert [call["target"] for call in calls] == ["legacy-user", "legacy-user"]


def test_paused_account_does_not_starve_another_lifecycle_account(tmp_path, controls):
    config, clock = controls
    config["notifications"]["routes"] = [config["notifications"]["routes"][1],
        {"id": "sy", "accounts": ["sy"], "provider": "feishu_app", "target": "sy"}]
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    _enqueue(repo, "lx", "one")
    _enqueue(repo, "sy", "two")
    clock[0] += 11
    route = receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=tmp_path)
    calls, send = _mock_transport()
    def dispatch():
        return lifecycle_outbox.dispatch_notification_batch_once(repo, route=route, now_ms=int(clock[0] * 1000),
            allowed_accounts=["lx", "sy"], send_fn=lambda payload: receipt.send_trade_lifecycle_outbox_payload(
                base=tmp_path, config=config, receipt_config={}, payload=payload, send_fn=send, normalize_fn=_normalizer))
    assert dispatch()["status"] == "explicit_failed"
    config["notifications"]["routes"] = config["notifications"]["routes"][1:]
    clock[0] += 61
    dispatch()
    clock[0] += 61
    dispatch()
    assert [call["target"] for call in calls] == ["B", "sy"]


@pytest.mark.parametrize("disabled", ["all_off", "empty_routes"])
def test_alert_failure_and_recovery_without_routes_keep_complete_delivery_facts(
    tmp_path, controls, monkeypatch, disabled,
):
    config, _ = controls
    if disabled == "all_off":
        config["channels"]["feishu"]["notify_enabled"] = False
    else:
        config["notifications"]["routes"] = []
    calls = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
        lambda provider: calls.append(provider) or pytest.fail("disabled transport"))
    fields = dict(base=tmp_path, config=config, unit="fixture", market="us",
        account="lx", failure_code="fixture", stage="fixture")
    assert system_alerts.report_system_failure(**fields, run_id="fixture", rc=1,
        first_error_at="2026-11-01T00:00:00+00:00", opend_login_state="unknown") == "suppressed"
    assert system_alerts.report_system_recovery(**fields) == "suppressed"
    state = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    incident, = state.values()
    assert incident["status"] == "recovered"
    assert incident["recovery_delivery"] == "disabled"
    assert incident["recovery_delivery_confirmed"] is False
    assert incident["recovery_provider"] == "fanout"
    assert incident["recovery_fallback_used"] is False
    assert not calls
