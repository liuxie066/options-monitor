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


def test_unconfirmed_alert_reserves_attempt_and_unconfigured_route_does_not(monkeypatch, tmp_path: Path) -> None:
    from src.application import system_alerts

    sends = []
    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", lambda _provider: SimpleNamespace(send_fn=lambda **kwargs: sends.append(kwargs) or {"delivery_confirmed": False}, normalize_fn=lambda **_: {}))
    fields = dict(base=tmp_path, unit="test.service", market="us", account="sy",
                  failure_code="TICK_TIMEOUT", stage="timeout", run_id="run-1", rc=124,
                  first_error_at="2026-09-24T00:00:00+00:00", opend_login_state="unknown")
    assert system_alerts.report_system_failure(config={}, **fields) == "unconfigured"
    assert not sends
    assert system_alerts.report_system_failure(config=_config(), **fields) == "unconfirmed"
    assert system_alerts.report_system_failure(config=_config(), **fields) == "suppressed"
    assert len(sends) == 1
    path = tmp_path / "output_shared" / "state" / "system_alerts.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    next(iter(state.values()))["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert system_alerts.report_system_failure(config=_config(), **fields) == "unconfirmed"
    assert sends[0]["idempotency_key"] == sends[1]["idempotency_key"]


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
    event = json.loads(events[0].read_text(encoding="utf-8"))
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
    status_path.write_text(json.dumps({"status": "listening", "last_heartbeat_utc": (now - timedelta(minutes=4)).isoformat()}), encoding="utf-8")
    args = dict(unit="options-monitor-trade-intake.service", market="us",
                config_path=str(config_path), runtime_root=tmp_path, now=now)
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
                config_path=str(config_path), runtime_root=tmp_path, now=now)
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: False) == 0
    assert "TRADE_INTAKE_PROCESS_DOWN" in sends[0]["message"]
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: True) == 0
    assert "已恢复" in sends[1]["message"]
    monkeypatch.setattr(service_failure_alert, "report_system_failure", lambda **_kwargs: (_ for _ in ()).throw(OSError("state")))
    assert service_failure_alert.check_trade_intake_heartbeat(**args, unit_active_fn=lambda _unit: False) == 1
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
