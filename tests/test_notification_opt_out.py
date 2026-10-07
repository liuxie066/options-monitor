from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.application.notification_delivery_route import notifications_enabled, resolve_notification_delivery_route

DISABLED = {"notifications": {"enabled": False, "provider": "feishu_app", "target": "ou_fixture"}}


def forbidden(*args, **kwargs):
    pytest.fail("disabled notification must not attempt delivery or resolve credentials")


def test_omission_and_explicit_disable_avoid_credentials(monkeypatch):
    import src.application.notification_delivery_route as mod
    assert not notifications_enabled({})
    assert not notifications_enabled({"notifications": {"provider": "feishu_app"}})
    monkeypatch.setattr(mod, "resolve_feishu_bot_send_target", forbidden)
    route = resolve_notification_delivery_route(config=DISABLED, route_resolver=forbidden)
    assert route["enabled"] is False and route["disabled_reason"] == "notifications_disabled"


def test_system_alert_cannot_fallback_when_disabled(tmp_path, monkeypatch):
    import src.application.system_alerts as mod
    monkeypatch.setattr(mod, "resolve_notification_delivery_route", forbidden)
    monkeypatch.setattr(mod, "_fallback_route", forbidden)
    result = mod._send(tmp_path, DISABLED, "fixture", "fixture-key")
    assert result["attempted"] is False and result["delivery_confirmed"] is False


def test_disabled_system_alert_records_failure_and_recovery_without_delivery_failure(tmp_path, monkeypatch, capsys):
    import json
    import src.application.system_alerts as mod
    monkeypatch.setattr(mod, "resolve_notification_delivery_route", forbidden)
    monkeypatch.setattr(mod, "_fallback_route", forbidden)
    fields = dict(base=tmp_path, config=DISABLED, unit="fixture.service", market="us", account="lx",
                  failure_code="FIXTURE_FAILURE", stage="heartbeat")
    assert mod.report_system_failure(**fields, run_id="fixture", rc=1, first_error_at="fixture",
                                     opend_login_state="unknown") == "suppressed"
    assert mod.system_alert_delivery_status(tmp_path)["status"] == "disabled"
    state_path = tmp_path / "output_shared/state/system_alerts.json"
    pending = json.loads(state_path.read_text())
    next(iter(pending.values()))["delivery"] = "unconfirmed"
    state_path.write_text(json.dumps(pending))
    # Turning off an existing unresolved failure bypasses delivery's silence
    # window, while retaining the business incident itself.
    assert mod.report_system_failure(**fields, run_id="fixture", rc=1, first_error_at="fixture",
                                     opend_login_state="unknown") == "suppressed"
    assert mod.system_alert_delivery_status(tmp_path)["status"] == "disabled"
    assert mod.report_system_recovery(**fields) == "suppressed"
    state = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    assert len(state) == 1
    incident = next(iter(state.values()))
    assert incident["status"] == "recovered"
    assert incident["recovery_delivery"] == "disabled"
    assert incident["recovery_delivery_confirmed"] is False
    assert incident["delivery_confirmed"] is False
    assert "UNCONFIRMED" not in capsys.readouterr().err
    # Restoring notifications must not replay this completed recovery.
    assert mod.report_system_recovery(**{**fields, "config": {}}) == "no_incident"


def test_trade_receipt_and_lifecycle_payload_do_not_claim_send(tmp_path):
    from src.application.trades.receipt import send_trade_intake_receipt, send_trade_lifecycle_outbox_payload
    result = send_trade_intake_receipt(base=tmp_path, config=DISABLED, receipt_config={},
                                      apply_changes=True, state={}, deal={}, result={},
                                      send_fn=forbidden, normalize_fn=forbidden, route_resolver=forbidden,
                                      inbox_path=tmp_path / "absent.sqlite3", inbox_id="fixture")
    assert result["reason"] == "notifications_disabled" and result["delivery_confirmed"] is False
    lifecycle = send_trade_lifecycle_outbox_payload(base=tmp_path, config=DISABLED, receipt_config={},
                                                   payload={}, send_fn=forbidden, route_resolver=forbidden)
    assert lifecycle["classification_evidence"]["preflight"] == "notifications_disabled"
    assert list(tmp_path.iterdir()) == []


def test_disabling_unknown_recovery_retires_delivery_meta_without_resending(tmp_path, monkeypatch):
    import json
    import src.application.system_alerts as mod
    calls = []
    def send(*args):
        calls.append(args)
        return {"delivery_confirmed": False, "provider": "fixture", "fallback_used": False, "attempted": True}
    monkeypatch.setattr(mod, "_send", send)
    fields = dict(base=tmp_path, config={"notifications": {"enabled": True}}, unit="fixture.service", market="us", account="lx",
                  failure_code="FIXTURE_FAILURE", stage="heartbeat")
    mod.report_system_failure(**fields, run_id="fixture", rc=1, first_error_at="fixture", opend_login_state="unknown")
    assert mod.report_system_recovery(**fields) == "unconfirmed"
    assert mod.report_system_recovery(**{**fields, "config": DISABLED}) == "suppressed"
    assert len(calls) == 2
    state = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    incident = next(item for item in state.values() if item["failure_code"] == "FIXTURE_FAILURE")
    meta = next(item for item in state.values() if item["failure_code"] == "SYSTEM_RECOVERY_DELIVERY_UNCONFIRMED")
    assert incident["recovery_delivery"] == "disabled"
    assert incident["recovery_delivery_confirmed"] is False
    assert meta["status"] == "retired"
    assert mod.report_system_recovery(**fields) == "no_incident"


def test_auto_close_receipt_preserves_identity_without_delivery(tmp_path):
    from src.application.positions.maintenance_receipt import send_auto_close_receipt
    result = send_auto_close_receipt(base=tmp_path, config=DISABLED, receipt_config={}, dry_run=False,
                                    result={}, receipt_key="fixture-key", send_fn=forbidden, route_resolver=forbidden)
    assert result["reason"] == "notifications_disabled"
    assert result["receipt_key"] == "fixture-key" and result["delivery_confirmed"] is False


def test_receipt_compensation_is_not_executable_while_disabled(tmp_path):
    from src.application.trades.receipt_compensation import _build_plan
    with pytest.raises(ValueError, match="notifications_disabled"):
        _build_plan(config=DISABLED, source={}, repo=None, account="lx", deal_ids=["fixture"],
                    reason="skipped_no_route", route_resolver=forbidden)


def test_scheduled_tick_retains_local_preparation_and_skips_delivery(tmp_path, monkeypatch):
    import src.application.tick_notification_flow as mod
    prepared = []
    def prepare(request):
        prepared.append(request.run_id)
        return mod.DailyBriefNotificationPreparation(
            prepared_messages=SimpleNamespace(messages_by_account={"lx": "fixture"}),
            lifecycles_by_account={}, delivery_keys_by_account={}, markets=("US",))
    monkeypatch.setattr(mod, "_prepare_daily_brief_notification", prepare)
    monkeypatch.setattr(mod, "resolve_notification_delivery_route", forbidden)
    monkeypatch.setattr(mod, "execute_per_account_delivery", forbidden)
    finishes = []
    monkeypatch.setattr(mod, "finalize_no_account_notification", lambda **kw: finishes.append(kw) or 0)
    completions = []
    request = mod.TickNotificationRequest(
        base=tmp_path, cfg_path=tmp_path / "config.us.json", state_path=tmp_path / "state.json",
        scheduler_schedule_key="us", base_cfg=DISABLED, run_id="fixture-disabled",
        runlog=SimpleNamespace(safe_event=lambda *a, **kw: None), results=[], tick_metrics={}, no_send=False,
        bj_tz=ZoneInfo("Asia/Shanghai"), audit_helper=SimpleNamespace(audit=lambda *a, **kw: None,
        guard_mark_success=lambda: None), vpy=Path("python3"), complete_tick_idempotency_fn=lambda **kw: completions.append(kw),
        markets_to_run=("US",), scheduler_markets=("US",), trigger_kind="scheduled")
    assert mod.run_tick_notification_flow(request) == 0
    assert prepared == ["fixture-disabled"]
    assert finishes[0]["reason"] == "notifications_disabled" and finishes[0]["no_send"] is True
    assert completions[0]["message"] == "notifications_disabled"
