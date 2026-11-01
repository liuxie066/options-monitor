"""System notices use selected routes, never credentials as channel intent."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.application import system_alerts
from domain.domain.multi_tick import notification_route_identity_hash


@pytest.mark.parametrize("outcome", ["missing_route", "missing_credentials", "explicit_failure", "unknown"])
def test_legacy_failure_and_recovery_never_fall_back_to_retained_credentials(
    tmp_path, monkeypatch, outcome,
):
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "retained-app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "retained-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "retained-user")
    calls = []

    def send(provider, **kwargs):
        calls.append(provider)
        if provider != "wechat_clawbot":
            return {"delivery_confirmed": True}
        if outcome == "missing_credentials":
            raise ValueError("WeChat ClawBot bot_token is missing")
        return {"delivery_confirmed": False,
                "explicit_pre_acceptance_failure": outcome == "explicit_failure"}

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda provider:
        SimpleNamespace(send_fn=lambda **kwargs: send(provider, **kwargs), normalize_fn=lambda send_result: send_result))
    config = {"notifications": {"enabled": True, "provider": "wechat_clawbot"}}
    if outcome != "missing_route":
        config["notifications"]["target"] = "wechat:fixture:ops"
    fields = dict(base=tmp_path, config=config, unit="fixture", market="us", account="alpha",
                  failure_code="FIXTURE", stage="fixture")
    result = system_alerts.report_system_failure(**fields, run_id="fixture", rc=1,
        first_error_at="2026-11-01T00:00:00+00:00", opend_login_state="unknown")
    assert result != "confirmed"
    assert system_alerts.report_system_recovery(**fields) != "confirmed"
    assert calls == ([] if outcome == "missing_route" else ["wechat_clawbot", "wechat_clawbot"])
    state = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    incident = next(value for value in state.values() if value.get("failure_code") == "FIXTURE")
    assert incident["fallback_used"] is False
    assert incident["recovery_fallback_used"] is False


def _global_config():
    return {"accounts": ["alpha", "beta"],
            "channels": {"feishu": {"enabled": True, "receive_enabled": False, "notify_enabled": True}},
            "notifications": {"enabled": True, "opend_alert_after_consecutive_failures": 1,
                "routes": [{"id": "shared", "provider": "feishu_app", "target": "ou_shared", "accounts": ["alpha", "beta"]},
                           {"id": "beta", "provider": "feishu_app", "target": "ou_beta", "accounts": ["beta"]}]}}


def test_global_opend_alert_and_recovery_use_configured_account_routes_once(tmp_path, monkeypatch):
    from src.application.multi_tick import opend_guard
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    calls = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda provider:
        SimpleNamespace(send_fn=lambda **kwargs: calls.append(kwargs) or {"delivery_confirmed": True,
            "attribution_route_key": notification_route_identity_hash(provider="feishu_app", channel="feishu_app",
                sender="fixture-app", recipient=kwargs["target"])}, normalize_fn=lambda send_result: send_result))
    config = _global_config()
    fields = dict(base=tmp_path, cfg=config, error_code="OPEND_LOGIN_INVALID", message_text="login invalid")
    assert opend_guard.send_opend_alert(**fields)
    assert not opend_guard.send_opend_alert(**fields)
    assert sorted(call["target"] for call in calls) == ["ou_beta", "ou_shared"]
    assert opend_guard.send_opend_recovery_notice(tmp_path, config)
    assert not opend_guard.send_opend_recovery_notice(tmp_path, config)
    assert sorted(call["target"] for call in calls) == ["ou_beta", "ou_beta", "ou_shared", "ou_shared"]
    assert len({call["idempotency_key"] for call in calls}) == 4
    assert all("alpha" not in call["message"] and "beta" not in call["message"] for call in calls)
    state = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    assert next(iter(state.values()))["status"] == "recovered"


def test_global_alert_missing_account_scope_is_unconfigured_not_disabled(tmp_path, monkeypatch):
    config = _global_config()
    config.pop("accounts")
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda *_: pytest.fail("no wildcard send"))
    assert system_alerts.report_system_failure(base=tmp_path, config=config, unit="opend_watchdog", market="all", account="all",
        failure_code="FIXTURE", stage="watchdog", run_id="", rc=1, first_error_at="fixture", opend_login_state="unknown") == "unconfigured"
    incident = next(iter(json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text()).values()))
    assert incident["reason"] == "system_alert_scope_unavailable"
    assert incident["delivery"] == "journal"
    assert system_alerts.system_alert_delivery_status(tmp_path)["status"] == "degraded"


def test_global_alert_failed_target_pauses_and_resumes_without_new_targets(tmp_path, monkeypatch):
    from copy import deepcopy
    from src.application import notification_fanout
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    clock = [2_000_000_000.0]
    monkeypatch.setattr(notification_fanout.time, "time", lambda: clock[0])
    calls = []

    def send(**kwargs):
        calls.append(deepcopy(kwargs))
        if kwargs["target"] == "ou_beta" and sum(item["target"] == "ou_beta" for item in calls) == 1:
            return {"explicit_pre_acceptance_failure": True}
        return {"delivery_confirmed": True, "attribution_route_key": notification_route_identity_hash(
            provider="feishu_app", channel="feishu_app", sender="fixture-app", recipient=kwargs["target"])}

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda provider:
        SimpleNamespace(send_fn=send, normalize_fn=lambda send_result: send_result))
    config = _global_config()
    initial = system_alerts._send(tmp_path, config, "global operational notice", "stable-global-key", account="all")
    assert initial["has_retryable_children"] and len(calls) == 2
    original = next(item for item in calls if item["target"] == "ou_beta")
    paused_config = deepcopy(config)
    paused_config["notifications"]["routes"] = config["notifications"]["routes"][:1]
    clock[0] += 601
    paused = system_alerts._send(tmp_path, paused_config, "changed text", "stable-global-key", account="all")
    assert not paused["attempted"] and len(calls) == 2
    config["notifications"]["routes"].append({"id": "new", "provider": "feishu_app", "target": "ou_new", "accounts": ["alpha"]})
    clock[0] += 601
    resumed = system_alerts._send(tmp_path, config, "changed text", "stable-global-key", account="all")
    assert resumed["delivery_confirmed"] and len(calls) == 3
    assert calls[-1] == original
    assert {child["spec"]["metadata"]["account"] for child in resumed["children"]} == {"alpha", "beta"}
