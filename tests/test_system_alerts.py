from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


def _config() -> dict:
    return {"notifications": {"provider": "wechat_clawbot", "target": "fixture-target"}}


def test_system_alert_is_deduped_and_recovers_once(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    sends = []

    def send(**kwargs):
        sends.append(kwargs)
        return {"delivery_confirmed": True}

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(send_fn=send, normalize_fn=lambda **_: {}))
    fields = dict(base=tmp_path, config=_config(), unit="test.service", market="hk", account="lx",
                  failure_code="TICK_EXEC_FAILED", stage="child_exit")
    failure = dict(**fields, run_id="run-1", rc=1, first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown")

    assert system_alerts.report_system_failure(**failure) == "confirmed"
    assert system_alerts.report_system_failure(**{**failure, "run_id": "run-2"}) == "suppressed"
    assert len(sends) == 1
    assert sends[0]["target"] == "fixture-target"
    assert "run-1" in sends[0]["message"]
    path = tmp_path / "output_shared" / "state" / "system_alerts.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    incident = next(iter(state.values()))
    reservation = incident["reserved_at"]
    incident["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert system_alerts.report_system_failure(**{**failure, "run_id": "run-2"}) == "confirmed"
    assert sends[0]["idempotency_key"] == sends[1]["idempotency_key"]
    assert next(iter(json.loads(path.read_text(encoding="utf-8")).values()))["reserved_at"] == reservation
    assert system_alerts.report_system_recovery(**fields) == "confirmed"
    assert system_alerts.report_system_recovery(**fields) == "no_incident"
    assert system_alerts.report_system_failure(**{**failure, "run_id": "run-3"}) == "confirmed"
    assert len(sends) == 4
    assert sends[0]["idempotency_key"] != sends[3]["idempotency_key"]


def test_unconfirmed_alert_and_unconfigured_route_reserve_attempts(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": False}, normalize_fn=lambda **_: {}))
    fields = dict(base=tmp_path, unit="test.service", market="us", account="sy",
                  failure_code="TICK_TIMEOUT", stage="timeout", run_id="run-1", rc=124,
                  first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown")
    assert system_alerts.report_system_failure(config={}, **fields) == "unconfigured"
    assert not sends
    assert system_alerts.report_system_failure(config=_config(), **fields) == "suppressed"
    path = tmp_path / "output_shared" / "state" / "system_alerts.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    next(iter(state.values()))["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert system_alerts.report_system_failure(config=_config(), **fields) == "unconfirmed"
    assert system_alerts.report_system_failure(config=_config(), **fields) == "suppressed"
    assert len(sends) == 1
    state = json.loads(path.read_text(encoding="utf-8"))
    next(iter(state.values()))["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert system_alerts.report_system_failure(config=_config(), **fields) == "unconfirmed"
    assert sends[0]["idempotency_key"] == sends[1]["idempotency_key"]


def test_unconfirmed_recovery_is_visible_and_retries_with_same_key(monkeypatch, tmp_path: Path, capsys) -> None:
    from src.application import system_alerts

    sends = []

    def send(**kwargs):
        sends.append(kwargs)
        return {"delivery_confirmed": len(sends) != 2}

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
                        lambda _provider: SimpleNamespace(send_fn=send, normalize_fn=lambda **_: {}))
    fields = dict(base=tmp_path, config=_config(), unit="test.service", market="hk", account="lx",
                  failure_code="TICK_EXEC_FAILED", stage="child_exit")
    assert system_alerts.report_system_failure(
        **fields, run_id="run-1", rc=1, first_error_at="2026-09-24T00:00:00+00:00",
        opend_login_state="unknown",
    ) == "confirmed"
    assert system_alerts.report_system_recovery(**fields) == "unconfirmed"
    assert "<3>SYSTEM_META_ALERT" in capsys.readouterr().err
    assert system_alerts.report_system_recovery(**fields) == "suppressed"
    assert len(sends) == 2

    path = tmp_path / "output_shared" / "state" / "system_alerts.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    key = system_alerts._fingerprint("test.service", "hk", "lx", "TICK_EXEC_FAILED", "child_exit")
    assert state[key]["status"] == "recovered"
    assert state[key]["recovery_delivery"] == "unknown"
    state[key]["recovery_last_attempt_at"] = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    assert system_alerts.report_system_recovery(**fields) == "suppressed"
    assert len(sends) == 2
    state = json.loads(path.read_text(encoding="utf-8"))
    state[key]["recovery_last_attempt_at"] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")

    assert system_alerts.report_system_recovery(**fields) == "confirmed"
    assert sends[1]["idempotency_key"] == sends[2]["idempotency_key"]
    assert "<4>SYSTEM_META_RECOVERY" in capsys.readouterr().err
    assert system_alerts.report_system_recovery(**fields) == "no_incident"
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state[key]["recovery_delivery"] == "confirmed"


def test_oversized_alert_state_is_pruned_before_new_alert(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    path = tmp_path / "output_shared" / "state" / "system_alerts.json"
    path.parent.mkdir(parents=True)
    old = {f"old-{index}": {"status": "recovered", "reserved_at": "2020-01-01T00:00:00+00:00", "padding": "x" * 1024}
           for index in range(1100)}
    path.write_text(json.dumps(old), encoding="utf-8")
    assert path.stat().st_size > 1024 * 1024
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {}))
    assert system_alerts.report_system_failure(
        base=tmp_path, config=_config(), unit="test.service", market="us", account="lx",
        failure_code="TICK_TIMEOUT", stage="timeout", run_id="run-1", rc=124,
        first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown",
    ) == "confirmed"
    assert len(sends) == 1
    state = json.loads(path.read_text(encoding="utf-8"))
    assert len(state) == system_alerts._MAX_INCIDENTS
    assert "old-0" not in state
    assert system_alerts._fingerprint("test.service", "us", "lx", "TICK_TIMEOUT", "timeout") in state


def test_corrupt_alert_state_fails_closed_without_resending(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    path = tmp_path / "output_shared" / "state" / "system_alerts.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: (_ for _ in ()).throw(AssertionError("must not send")))
    with pytest.raises(json.JSONDecodeError):
        system_alerts.report_system_failure(
            base=tmp_path, config=_config(), unit="test.service", market="us", account="lx",
            failure_code="TICK_TIMEOUT", stage="timeout", run_id="run-1", rc=124,
            first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown",
        )


def test_journal_meta_signal_dedupes_and_reports_recovery(tmp_path: Path, capsys) -> None:
    from src.application import system_alerts

    fields = dict(base=tmp_path, unit="options-monitor-tick-us.service", market="us",
                  account="lx", failure_code="NOTIFICATION_DELIVERY_UNCONFIRMED",
                  stage="delivery", run_id="run-1", reason="send_unconfirmed")
    assert system_alerts.report_system_meta_signal(**fields, degraded=True) == "signaled"
    assert system_alerts.report_system_meta_signal(**fields, degraded=True) == "suppressed"
    assert capsys.readouterr().err.count("<3>SYSTEM_META_ALERT") == 1
    assert system_alerts.report_system_meta_signal(**fields, degraded=False) == "recovered"
    assert system_alerts.report_system_meta_signal(**fields, degraded=False) == "no_incident"
    assert capsys.readouterr().err.count("<4>SYSTEM_META_RECOVERY") == 1
    assert system_alerts.report_system_meta_signal(**fields, degraded=True) == "signaled"
    state = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    assert next(iter(state.values()))["status"] == "failed"


def test_missing_primary_route_uses_independent_feishu_credentials_and_stable_fallback_key(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "fixture-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "fixture-open-id")
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
                        lambda provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append((provider, kwargs)) or {"delivery_confirmed": True},
                                                         normalize_fn=lambda **_: {}))
    fields = dict(base=tmp_path, config={}, unit="test.service", market="hk", account="lx",
                  failure_code="TICK_TIMEOUT", stage="timeout", run_id="run-1", rc=124,
                  first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown")
    (tmp_path / "output_runs" / "run-1").mkdir(parents=True)
    assert system_alerts.report_system_failure(**fields) == "confirmed"
    assert sends[0][0] == "feishu_app"
    assert sends[0][1]["target"] == "fixture-open-id"
    assert sends[0][1]["notifications"] == {}
    state_path = tmp_path / "output_shared/state/system_alerts.json"
    incident = next(iter(json.loads(state_path.read_text()).values()))
    primary_key = "om-" + system_alerts.hashlib.sha256(
        (system_alerts._fingerprint("test.service", "hk", "lx", "TICK_TIMEOUT", "timeout")
         + incident["reserved_at"]).encode()).hexdigest()[:32]
    assert sends[0][1]["idempotency_key"] != primary_key
    assert (incident["fallback_used"], incident["provider"], incident["delivery_confirmed"]) == (True, "feishu_app", True)
    audit = [json.loads(line) for line in (tmp_path / "output_runs/run-1/state/audit_events.jsonl").read_text().splitlines()]
    assert audit[-1]["fallback_used"] is True
    assert audit[-1]["extra"]["provider"] == "feishu_app"
    assert audit[-1]["extra"]["delivery_confirmed"] is True
    assert system_alerts.report_system_failure(**fields) == "suppressed"
    state = json.loads(state_path.read_text())
    next(iter(state.values()))["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    state_path.write_text(json.dumps(state))
    assert system_alerts.report_system_failure(**fields) == "confirmed"
    assert sends[1][1]["idempotency_key"] == sends[0][1]["idempotency_key"]


def test_missing_route_without_fallback_stays_local_and_degraded(monkeypatch, tmp_path: Path, capsys) -> None:
    from src.application import system_alerts

    for name in ("OM_FEISHU_BOT_APP_ID", "OM_FEISHU_BOT_APP_SECRET", "OM_FEISHU_BOT_USER_OPEN_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
                        lambda _provider: (_ for _ in ()).throw(AssertionError("must not send")))
    fields = dict(base=tmp_path, config={}, unit="test.service", market="hk", account="lx",
                  failure_code="TICK_TIMEOUT", stage="timeout", run_id="run-1", rc=124,
                  first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown")
    assert system_alerts.report_system_failure(**fields) == "unconfigured"
    assert "<3>SYSTEM_ALERT_UNCONFIRMED" in capsys.readouterr().err
    assert system_alerts.system_alert_delivery_status(tmp_path)["status"] == "degraded"
    assert system_alerts.report_system_failure(**fields) == "suppressed"


def test_explicit_primary_unavailability_uses_distinct_fallback_key(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "fixture-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "fixture-open-id")
    sends = []
    def send(provider, **kwargs):
        sends.append((provider, kwargs["idempotency_key"]))
        return ({"delivery_confirmed": False, "explicit_pre_acceptance_failure": True}
                if provider == "wechat_clawbot" else {"delivery_confirmed": True})

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
                        lambda provider: SimpleNamespace(send_fn=lambda **kwargs: send(provider, **kwargs),
                                                         normalize_fn=lambda **_: {}))
    assert system_alerts.report_system_failure(
        base=tmp_path, config=_config(), unit="test.service", market="hk", account="lx",
        failure_code="TICK_TIMEOUT", stage="timeout", run_id="run-1", rc=124,
        first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown",
    ) == "confirmed"
    assert [provider for provider, _key in sends] == ["wechat_clawbot", "feishu_app"]
    assert sends[0][1] != sends[1][1]


def test_fallback_failure_does_not_cascade_and_meta_signal_is_single_attempt(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "fixture-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "fixture-open-id")
    sends = []
    def fail_send(provider, **_kwargs):
        sends.append(provider)
        raise RuntimeError("fixture provider failure")

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
                        lambda provider: SimpleNamespace(send_fn=lambda **kwargs: fail_send(provider, **kwargs),
                                                         normalize_fn=lambda **_: {}))
    fields = dict(base=tmp_path, config={}, unit="test.service", market="hk", account="lx",
                  failure_code="NOTIFICATION_DELIVERY_UNCONFIRMED", stage="delivery", run_id="run-1", degraded=True,
                  reason="route_missing", external=True)
    assert system_alerts.report_system_meta_signal(**fields) == "signaled"
    assert system_alerts.report_system_meta_signal(**fields) == "suppressed"
    assert sends == ["feishu_app"]
    incident = next(iter(json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text()).values()))
    assert incident["fallback_used"] is True
    assert incident["delivery_confirmed"] is False


def test_missing_feishu_route_uses_single_independent_wechat_binding(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    state_dir = tmp_path / "output_shared/state/channels/wechat_clawbot/default"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(json.dumps({"bot_token": "fixture-token"}))
    (state_dir / "bindings.json").write_text(json.dumps({"bindings": {"ops": {
        "to_user_id": "fixture-user", "context_token": "fixture-context"}}}))
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
                        lambda provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append((provider, kwargs)) or {"delivery_confirmed": True},
                                                         normalize_fn=lambda **_: {}))
    fields = dict(base=tmp_path, config={"notifications": {"provider": "feishu_app"}},
                  unit="test.service", market="hk", account="lx", failure_code="TICK_TIMEOUT",
                  stage="timeout", run_id="run-1", rc=124,
                  first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown")
    assert system_alerts.report_system_failure(**fields) == "confirmed"
    assert sends[0][0] == "wechat_clawbot"
    assert sends[0][1]["target"] == "wechat:default:ops"


def test_recovery_fallback_retries_after_600s_with_same_key_and_current_evidence(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "fixture-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "fixture-open-id")
    sends = []
    def send(provider, **kwargs):
        sends.append((provider, kwargs))
        return {"delivery_confirmed": provider == "wechat_clawbot" or len(sends) == 3}

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter",
                        lambda provider: SimpleNamespace(send_fn=lambda **kwargs: send(provider, **kwargs),
                                                         normalize_fn=lambda **_: {}))
    fields = dict(base=tmp_path, unit="test.service", market="hk", account="lx",
                  failure_code="TICK_TIMEOUT", stage="timeout")
    (tmp_path / "output_runs/run-1").mkdir(parents=True)
    assert system_alerts.report_system_failure(
        **fields, config=_config(), run_id="run-1", rc=124,
        first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown",
    ) == "confirmed"
    assert system_alerts.report_system_recovery(**fields, config={}) == "unconfirmed"
    state_path = tmp_path / "output_shared/state/system_alerts.json"
    state = json.loads(state_path.read_text())
    incident = state[system_alerts._fingerprint("test.service", "hk", "lx", "TICK_TIMEOUT", "timeout")]
    assert (incident["fallback_used"], incident["provider"], incident["delivery_confirmed"]) == (True, "feishu_app", False)
    assert incident["failure_provider"] == "wechat_clawbot"
    incident["recovery_last_attempt_at"] = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
    state_path.write_text(json.dumps(state))
    assert system_alerts.report_system_recovery(**fields, config={}) == "suppressed"
    incident["recovery_last_attempt_at"] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    state_path.write_text(json.dumps(state))
    assert system_alerts.report_system_recovery(**fields, config={}) == "confirmed"
    assert sends[1][1]["idempotency_key"] == sends[2][1]["idempotency_key"]
    state = json.loads(state_path.read_text())
    recovered = state[system_alerts._fingerprint("test.service", "hk", "lx", "TICK_TIMEOUT", "timeout")]
    assert (recovered["fallback_used"], recovered["provider"], recovered["delivery_confirmed"]) == (True, "feishu_app", True)
    audit = [json.loads(line) for line in (tmp_path / "output_runs/run-1/state/audit_events.jsonl").read_text().splitlines()]
    assert audit[-1]["action"] == "recovery_delivery"
    assert audit[-1]["fallback_used"] is True
    assert audit[-1]["extra"]["delivery_confirmed"] is True


def test_tick_failure_records_run_and_alerts_once(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts
    from src.application.tick_cron import run_tick_cron

    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {}))

    for _ in range(2):
        assert run_tick_cron(
            market="hk", accounts=["lx"], config_path=str(config_path),
            lock_path=str(tmp_path / "tick.lock"), runtime_root=tmp_path,
            run_cmd=lambda command, **_kwargs: subprocess.CompletedProcess(command, 78),
            preflight_config_fn=None, environ={},
        ) == 78
    assert len(sends) == 1
    assert "TICK_EXEC_FAILED" in sends[0]["message"]
    events = list((tmp_path / "output_runs").glob("*/state/audit_events.jsonl"))
    assert len(events) == 2
    event = json.loads(events[0].read_text(encoding="utf-8").splitlines()[0])
    assert event["extra"]["account"] == "lx"
    assert event["extra"]["rc"] == 78


def test_service_failure_handler_routes_terminal_state_without_broker(monkeypatch, tmp_path: Path) -> None:
    from src.application import service_failure_alert

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    calls = []
    monkeypatch.setattr(service_failure_alert, "accounts_from_config_path", lambda *_args, **_kwargs: ["lx"])
    monkeypatch.setattr(service_failure_alert, "report_system_failure", lambda **kwargs: calls.append(kwargs) or "confirmed")

    assert service_failure_alert.alert_failed_service(
        unit="options-monitor-trade-intake.service", market="us",
        config_path=str(config_path), runtime_root=tmp_path,
    ) == 0
    assert calls[0]["failure_code"] == "SERVICE_TERMINAL_FAILURE"
    assert calls[0]["stage"] == "unit_failed"
    assert calls[0]["account"] == "lx"
    assert calls[0]["rc"] == -1


def test_service_failure_handler_logs_alert_infrastructure_failure(monkeypatch, tmp_path: Path, capsys) -> None:
    from src.application import service_failure_alert

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    monkeypatch.setattr(service_failure_alert, "accounts_from_config_path", lambda *_args, **_kwargs: ["lx"])
    monkeypatch.setattr(service_failure_alert, "report_system_failure", lambda **_kwargs: (_ for _ in ()).throw(OSError("state unavailable")))
    assert service_failure_alert.alert_failed_service(
        unit="options-monitor-trade-intake.service", market="us",
        config_path=str(config_path), runtime_root=tmp_path,
    ) == 1
    assert capsys.readouterr().out.strip() == "<3>SERVICE_ALERT_INFRA_FAILED"


@pytest.mark.parametrize("reason_code", [
    "OPEND_NEEDS_PHONE_VERIFY", "OPEND_LOGIN_INVALID", "OPEND_NEEDS_PIC_VERIFY",
])
def test_auth_terminal_service_failure_alerts_once_with_reason_code(
    monkeypatch, tmp_path: Path, reason_code: str,
) -> None:
    from src.application import service_failure_alert, system_alerts

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    status_path = tmp_path / "intake-status.json"
    status_path.write_text(json.dumps({"status": "blocked", "reason_code": reason_code}), encoding="utf-8")
    monkeypatch.setattr(service_failure_alert, "accounts_from_config_path", lambda *_args, **_kwargs: ["lx"])
    monkeypatch.setattr(service_failure_alert, "resolve_trade_intake_config", lambda _cfg: {
        "sources": [{"account": "lx", "status_path": str(status_path)}],
    })
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {}))

    for _ in range(2):
        assert service_failure_alert.alert_failed_service(
            unit="options-monitor-trade-intake.service", market="us",
            config_path=str(config_path), runtime_root=tmp_path,
        ) == 0
    assert len(sends) == 1
    assert reason_code in sends[0]["message"]


def test_trade_intake_heartbeat_stale_and_terminal_incidents_recover_once(monkeypatch, tmp_path: Path) -> None:
    from src.application import service_failure_alert, system_alerts

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    status_path = tmp_path / "intake-status.json"
    monkeypatch.setattr(service_failure_alert, "resolve_trade_intake_config", lambda _cfg: {
        "sources": [{"account": "lx", "status_path": str(status_path)}],
    })
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {}))
    now = datetime(2026, 9, 24, 2, tzinfo=timezone.utc)
    stale_age = service_failure_alert._HEARTBEAT_STALE_SECONDS + 60
    status_path.write_text(json.dumps({"status": "listening", "last_heartbeat_utc": (now - timedelta(seconds=stale_age)).isoformat()}), encoding="utf-8")
    args = dict(unit="options-monitor-trade-intake.service", market="us",
                config_path=str(config_path), runtime_root=tmp_path, now=now,
                disk_usage_fn=lambda _path: SimpleNamespace(total=100, used=10))
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert len(sends) == 1
    assert "TRADE_INTAKE_HEARTBEAT_STALE" in sends[0]["message"]
    terminal = dict(base=tmp_path, config=_config(), unit=args["unit"], market="us", account="lx",
                    failure_code="SERVICE_TERMINAL_FAILURE", stage="unit_failed")
    assert system_alerts.report_system_failure(**terminal, run_id="run-1", rc=78,
        first_error_at=now.isoformat(), opend_login_state="unknown") == "confirmed"
    status_path.write_text(json.dumps({"status": "listening", "last_heartbeat_utc": now.isoformat()}), encoding="utf-8")
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert len(sends) == 4
    assert sum("已恢复" in send["message"] for send in sends) == 2


def test_trade_intake_heartbeat_ignores_transient_stage_labels(monkeypatch, tmp_path: Path) -> None:
    """A healthy intake mid-iteration must not be reported stale.

    Production 2026-09-24: the listener writes `status="starting"` /
    `stage="receipt_recovery"` at the top of every work-loop iteration, runs the
    iteration's heavy work, then writes `listening` at the end of that same
    iteration — so a once-a-minute sample almost always caught "starting" and
    paged the operator every silence window (10 min), with the heartbeat only 41s
    old when it fired. Liveness is the unit being active plus a fresh heartbeat;
    the stage labels churn every iteration and carry no liveness meaning; only the
    labels the writer reserves for a source that is not working count against it.
    """
    from src.application import service_failure_alert, system_alerts

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    status_path = tmp_path / "intake-status.json"
    now = datetime(2026, 9, 24, 16, 3, 43, tzinfo=timezone.utc)
    status_path.write_text(json.dumps({
        "status": "starting",
        "stage": "receipt_recovery",
        "reason_code": "none",
        "last_heartbeat_utc": (now - timedelta(seconds=41)).isoformat(),
    }), encoding="utf-8")
    monkeypatch.setattr(service_failure_alert, "resolve_trade_intake_config", lambda _cfg: {
        "sources": [{"account": "lx", "status_path": str(status_path)}],
    })
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(
        send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {},
    ))
    args = dict(unit="options-monitor-trade-intake.service", market="us",
                config_path=str(config_path), runtime_root=tmp_path, now=now,
                disk_usage_fn=lambda _path: SimpleNamespace(total=100, used=10))
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert sends == []


@pytest.mark.parametrize(("status_label", "expected_sends"), [
    ("listening", 0),
    ("starting", 0),
    ("once", 0),
    ("reconnecting", 1),
    ("blocked", 1),
])
def test_trade_intake_heartbeat_reads_the_status_vocabulary(monkeypatch, tmp_path: Path, status_label: str, expected_sends: int) -> None:
    """A not-working label must still alert, and the working ones must not.

    The 3.7.1 gate required `status == "listening"` outright. Deleting the label
    entirely would have gone the other way and reported *recovery* for a source
    that is not working: `reconnecting` is written when the listener throws
    (OpenD down), and the retry loop's recovery tick refreshes the heartbeat while
    it backs off, so freshness alone cannot see that outage.
    """
    from src.application import service_failure_alert, system_alerts

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    status_path = tmp_path / "intake-status.json"
    now = datetime(2026, 9, 24, 16, 3, 43, tzinfo=timezone.utc)
    status_path.write_text(json.dumps({
        "status": status_label,
        "stage": "listener_exception" if status_label == "reconnecting" else "heartbeat",
        "reason_code": "none",
        "last_heartbeat_utc": (now - timedelta(seconds=5)).isoformat(),
    }), encoding="utf-8")
    monkeypatch.setattr(service_failure_alert, "resolve_trade_intake_config", lambda _cfg: {
        "sources": [{"account": "lx", "status_path": str(status_path)}],
    })
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(
        send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {},
    ))
    args = dict(unit="options-monitor-trade-intake.service", market="us",
                config_path=str(config_path), runtime_root=tmp_path, now=now,
                disk_usage_fn=lambda _path: SimpleNamespace(total=100, used=10))
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert len(sends) == expected_sends, status_label
    if expected_sends:
        assert "TRADE_INTAKE_HEARTBEAT_STALE" in sends[0]["message"]


def test_trade_intake_heartbeat_rejects_a_future_heartbeat(monkeypatch, tmp_path: Path) -> None:
    """A clock-skewed heartbeat must not read as fresh forever.

    The lower bound is the only guard against a timestamp written ahead of the
    checker, and reading the file through `parse_utc` widened what parses at all,
    so pin the bound itself.
    """
    from src.application import service_failure_alert, system_alerts

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    status_path = tmp_path / "intake-status.json"
    now = datetime(2026, 9, 24, 16, 3, 43, tzinfo=timezone.utc)
    status_path.write_text(json.dumps({
        "status": "listening",
        "last_heartbeat_utc": (now + timedelta(seconds=61)).isoformat(),
    }), encoding="utf-8")
    monkeypatch.setattr(service_failure_alert, "resolve_trade_intake_config", lambda _cfg: {
        "sources": [{"account": "lx", "status_path": str(status_path)}],
    })
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(
        send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {},
    ))
    args = dict(unit="options-monitor-trade-intake.service", market="us",
                config_path=str(config_path), runtime_root=tmp_path, now=now,
                disk_usage_fn=lambda _path: SimpleNamespace(total=100, used=10))
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert len(sends) == 1
    assert "TRADE_INTAKE_HEARTBEAT_STALE" in sends[0]["message"]


def test_trade_intake_heartbeat_window_clears_measured_worst_case_yet_still_alerts(monkeypatch, tmp_path: Path) -> None:
    """Cover one full work-loop iteration, and keep firing past that window.

    Widening the window must not be mistaken for silencing the alert: the last
    assertion is the anti-regression control proving a genuinely stale heartbeat
    still reaches the operator.
    """
    from src.application import service_failure_alert, system_alerts

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    status_path = tmp_path / "intake-status.json"
    now = datetime(2026, 9, 24, 16, 3, 43, tzinfo=timezone.utc)
    monkeypatch.setattr(service_failure_alert, "resolve_trade_intake_config", lambda _cfg: {
        "sources": [{"account": "lx", "status_path": str(status_path)}],
    })
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(
        send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {},
    ))
    args = dict(unit="options-monitor-trade-intake.service", market="us",
                config_path=str(config_path), runtime_root=tmp_path, now=now,
                disk_usage_fn=lambda _path: SimpleNamespace(total=100, used=10))
    worst_measured_iteration = 583  # production 2026-09-24, 16h of audit records (n=163)
    for age_seconds, expected_sends in (
        (worst_measured_iteration, 0),
        (service_failure_alert._HEARTBEAT_STALE_SECONDS + 1, 1),
    ):
        status_path.write_text(json.dumps({
            "status": "listening",
            "last_heartbeat_utc": (now - timedelta(seconds=age_seconds)).isoformat(),
        }), encoding="utf-8")
        assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
        assert len(sends) == expected_sends, f"age={age_seconds}s"
    assert "TRADE_INTAKE_HEARTBEAT_STALE" in sends[0]["message"]


def test_trade_intake_heartbeat_distinguishes_process_down_and_infra_failure(monkeypatch, tmp_path: Path, capsys) -> None:
    from src.application import service_failure_alert, system_alerts

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    status_path = tmp_path / "intake-status.json"
    now = datetime(2026, 9, 24, 2, tzinfo=timezone.utc)
    status_path.write_text(json.dumps({"status": "listening", "last_heartbeat_utc": now.isoformat()}), encoding="utf-8")
    monkeypatch.setattr(service_failure_alert, "resolve_trade_intake_config", lambda _cfg: {
        "sources": [{"account": "lx", "status_path": str(status_path)}],
    })
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {}))
    args = dict(unit="options-monitor-trade-intake.service", market="us",
                config_path=str(config_path), runtime_root=tmp_path, now=now,
                disk_usage_fn=lambda _path: SimpleNamespace(total=100, used=10))
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: False) == 0
    assert "TRADE_INTAKE_PROCESS_DOWN" in sends[0]["message"]
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert "已恢复" in sends[1]["message"]
    monkeypatch.setattr(service_failure_alert, "report_system_failure", lambda **_kwargs: (_ for _ in ()).throw(OSError("state")))
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: False) == 1
    assert "<3>INTAKE_HEARTBEAT_ALERT_INFRA_FAILED" in capsys.readouterr().out


def test_root_disk_threshold_alerts_repeat_safely_and_recover(monkeypatch, tmp_path: Path, capsys) -> None:
    from src.application import service_failure_alert, system_alerts

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    status_path = tmp_path / "intake-status.json"
    now = datetime(2026, 9, 24, 2, tzinfo=timezone.utc)
    status_path.write_text(json.dumps({"status": "listening", "last_heartbeat_utc": now.isoformat()}), encoding="utf-8")
    monkeypatch.setattr(service_failure_alert, "resolve_trade_intake_config", lambda _cfg: {
        "sources": [{"account": "lx", "status_path": str(status_path)}],
    })
    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(
        send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": True}, normalize_fn=lambda **_: {},
    ))
    used = [84]
    args = dict(unit="options-monitor-trade-intake.service", market="us",
                config_path=str(config_path), runtime_root=tmp_path, now=now,
                unit_active_fn=lambda _unit: True,
                disk_usage_fn=lambda path: SimpleNamespace(total=100, used=used[0]))
    for level, expected_sends in ((84, 0), (85, 1), (85, 1), (90, 2), (90, 2), (80, 4), (80, 4)):
        used[0] = level
        assert service_failure_alert.check_trade_intake_heartbeat(**args) == 0
        assert len(sends) == expected_sends
    assert sum("已恢复" in item["message"] for item in sends) == 2
    output = capsys.readouterr().out
    assert output.count("<4>ROOT_DISK_USAGE_85") == 1
    assert output.count("<3>ROOT_DISK_USAGE_90") == 1
    args["disk_usage_fn"] = lambda _path: (_ for _ in ()).throw(OSError("disk fixture unavailable"))
    assert service_failure_alert.check_trade_intake_heartbeat(**args) == 1
    assert "<3>INTAKE_HEARTBEAT_ALERT_INFRA_FAILED" in capsys.readouterr().out


def test_service_failure_alert_cli_dispatch(monkeypatch, tmp_path: Path) -> None:
    from src.interfaces.cli import run_ops
    from src.interfaces.cli.main import parse_args

    calls = []
    monkeypatch.setattr(run_ops, "alert_failed_service", lambda **kwargs: calls.append(kwargs) or 0)
    args = parse_args(["run", "service-failure-alert", "--unit", "options-monitor-trade-intake.service",
                       "--market", "us", "--config", str(tmp_path / "config.json"),
                       "--runtime-root", str(tmp_path)])
    assert run_ops.handle_run_command(args) == 0
    assert calls[0]["unit"] == "options-monitor-trade-intake.service"


def test_trade_intake_heartbeat_cli_dispatch(monkeypatch, tmp_path: Path) -> None:
    from src.interfaces.cli import run_ops
    from src.interfaces.cli.main import parse_args

    calls = []
    monkeypatch.setattr(run_ops, "check_trade_intake_heartbeat", lambda **kwargs: calls.append(kwargs) or 0)
    args = parse_args(["run", "trade-intake-heartbeat-check", "--unit", "options-monitor-trade-intake.service",
                       "--market", "us", "--config", str(tmp_path / "config.json"),
                       "--runtime-root", str(tmp_path)])
    assert run_ops.handle_run_command(args) == 0
    assert calls[0]["unit"] == "options-monitor-trade-intake.service"
