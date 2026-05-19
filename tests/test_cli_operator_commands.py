from __future__ import annotations

import os
import json
from pathlib import Path


def _read_json_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_top_level_doctor_wraps_healthcheck(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _healthcheck(**kwargs):
        calls.append(kwargs)
        return {"tool_name": "healthcheck", "ok": True, "data": {"status": "pass"}}

    monkeypatch.setattr(cli, "run_healthcheck", _healthcheck)

    rc = cli.main(["doctor", "--config-key", "us", "--accounts", "lx", "sy"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "doctor"
    assert payload["ok"] is True
    assert payload["data"]["healthcheck"]["tool_name"] == "healthcheck"
    assert calls == [{
        "config_key": "us",
        "config_path": None,
        "accounts": ["lx", "sy"],
        "opend_telnet_host": None,
        "opend_telnet_port": None,
    }]


def _runtime_status_envelope(*, ok: bool = True) -> dict:
    return {
        "tool_name": "runtime_status",
        "ok": ok,
        "data": {
            "summary": {
                "ok": True,
                "warning_count": 0,
                "latest_status": "ok",
                "freshness_status": "fresh",
                "ledger_status": "ok",
                "ledger_fail_closed": False,
                "ledger_sqlite_path": "output_shared/state/option_positions.sqlite3",
                "ledger_trade_event_count": 3,
                "ledger_position_lot_count": 2,
            },
            "freshness": {
                "status": "fresh",
                "age_seconds": 42,
                "max_age_minutes": 60,
                "latest_source": "latest_run.last_run",
            },
            "config": {
                "config_key": "us",
                "config_path": ".../config.us.json",
                "accounts": ["lx", "sy"],
            },
            "latest_run_selection": {
                "found": True,
                "path": "output_runs/run-1",
                "source": "requested",
            },
            "latest_scanned_run_selection": {
                "found": True,
                "path": "output_runs/run-1",
                "source": "requested",
            },
            "notification_diagnosis": {
                "status": "sent",
                "final_reason": "confirmed",
                "notification_route": {
                    "provider": "openclaw",
                    "channel": "openclaw-weixin",
                    "target_configured": True,
                },
                "send_attempted_count": 1,
                "send_confirmed_count": 1,
                "send_failed_count": 0,
            },
            "ledger_store": {
                "trade_event_count": 3,
                "position_lot_count": 2,
                "sqlite_path": "output_shared/state/option_positions.sqlite3",
            },
            "projection_verify": {
                "exists": True,
                "ok": True,
                "mode": "full",
                "path": "projection_verify.latest.json",
            },
            "trade_intake": {
                "enabled": True,
                "mode": "apply",
                "summary": {
                    "listener_status": "listening",
                    "processed_count": 4,
                    "failed_count": 0,
                    "unresolved_count": 0,
                    "receipt_count": 2,
                    "receipt_confirmed_count": 2,
                    "receipt_failed_count": 0,
                },
            },
            "required_data_prefetch": {
                "available": True,
                "available_account_count": 2,
                "account_count": 2,
                "total_opend_calls": 4,
                "total_rate_gate_wait_sec": 0.5,
                "total_errors": 0,
                "primary_bottleneck": None,
            },
            "latest_scanned_run_required_data_prefetch": {
                "available": True,
                "available_account_count": 2,
                "account_count": 2,
                "total_opend_calls": 4,
                "total_rate_gate_wait_sec": 0.5,
                "total_errors": 0,
                "primary_bottleneck": None,
            },
            "service_upgrade": {"status": "current", "target_version": None},
        },
        "warnings": [],
    }


def test_top_level_status_prints_human_summary(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[tuple[str, dict]] = []

    def _execute_tool(name: str, payload: dict) -> dict:
        calls.append((name, payload))
        return _runtime_status_envelope()

    monkeypatch.setattr(cli, "execute_tool", _execute_tool)

    rc = cli.main(["status", "--config-key", "us", "--accounts", "lx", "sy", "--run-id", "run-1"])
    out = capsys.readouterr().out

    assert rc == 0
    assert calls == [("runtime_status", {"config_key": "us", "accounts": ["lx", "sy"], "run_id": "run-1"})]
    assert "options-monitor status" in out
    assert "overall: OK freshness=fresh warnings=0 latest_status=ok" in out
    assert "config: key=us path=.../config.us.json accounts=lx, sy" in out
    assert "notifications: status=sent reason=confirmed route=openclaw/openclaw-weixin target=yes sent=1 confirmed=1 failed=0" in out
    assert "ledger: status=ok fail_closed=no events=3 lots=2 sqlite=output_shared/state/option_positions.sqlite3" in out


def test_top_level_status_json_prints_raw_runtime_status(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    def _execute_tool(name: str, payload: dict) -> dict:
        assert name == "runtime_status"
        assert payload == {"profile_path": "service.profile.json"}
        return _runtime_status_envelope()

    monkeypatch.setattr(cli, "execute_tool", _execute_tool)

    rc = cli.main(["status", "--profile-path", "service.profile.json", "--json"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "runtime_status"
    assert payload["data"]["summary"]["latest_status"] == "ok"


def test_top_level_status_returns_error_when_runtime_status_tool_fails(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    def _execute_tool(_name: str, _payload: dict) -> dict:
        return {
            "tool_name": "runtime_status",
            "ok": False,
            "data": {},
            "warnings": ["read failed"],
            "error": {"code": "RUNTIME_STATUS_ERROR", "message": "cannot read profile"},
        }

    monkeypatch.setattr(cli, "execute_tool", _execute_tool)

    rc = cli.main(["status", "--config-key", "us"])
    out = capsys.readouterr().out

    assert rc == 2
    assert "overall: FAIL" in out
    assert "error: RUNTIME_STATUS_ERROR cannot read profile" in out
    assert "- read failed" in out


def _write_run_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_top_level_runs_lists_runtime_runs(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    runs_root = tmp_path / "output_runs"
    scan_run = runs_root / "run-scan"
    skip_run = runs_root / "run-skip"
    _write_run_json(
        scan_run / "state" / "tick_metrics.json",
        {
            "ran_scan": True,
            "sent": True,
            "accounts": [{"account": "lx", "ran_scan": True}],
            "reason": "sent",
        },
    )
    _write_run_json(
        skip_run / "state" / "tick_metrics.json",
        {
            "sent": False,
            "scheduler_decision": {
                "should_run_scan": False,
                "should_notify": False,
                "reason": "market closed",
            },
            "accounts": [{"account": "sy", "ran_scan": False}],
            "reason": "no_account_notification",
        },
    )
    os.utime(skip_run, (100, 100))
    os.utime(scan_run, (200, 200))

    rc = cli.main(["runs", "--runs-root", str(runs_root), "--limit", "2"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "options-monitor runs" in out
    assert "count: 2/2 limit=2 scanned_only=no" in out
    assert "- run-scan " in out
    assert "status=scan scan=yes sent=yes accounts=lx reason=sent" in out
    assert "- run-skip " in out
    assert "status=skipped scan=no sent=no accounts=sy reason=no_account_notification" in out


def test_top_level_runs_json_can_select_run(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    runs_root = tmp_path / "output_runs"
    _write_run_json(
        runs_root / "run-1" / "state" / "last_run.json",
        {"schema_kind": "option_positions_auto_close_expired_run", "status": "skipped", "accounts": ["lx"]},
    )

    rc = cli.main(["runs", "--runs-root", str(runs_root), "--run-id", "run-1", "--json"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "runs"
    assert payload["data"]["summary"]["requested_found"] is True
    assert payload["data"]["selected_run"]["run_id"] == "run-1"
    assert payload["data"]["selected_run"]["status"] == "skipped"


def test_top_level_runs_missing_selected_run_returns_error(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    runs_root = tmp_path / "output_runs"
    runs_root.mkdir()

    rc = cli.main(["runs", "--runs-root", str(runs_root), "--run-id", "missing"])
    out = capsys.readouterr().out

    assert rc == 2
    assert "options-monitor runs" in out
    assert "requested: not found missing" in out
    assert "count: 0/0" in out


def test_top_level_logs_tails_run_audit(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    runs_root = tmp_path / "output_runs"
    audit = runs_root / "run-1" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text('{"message":"first"}\n{"message":"second"}\n', encoding="utf-8")

    rc = cli.main(["logs", "--runs-root", str(runs_root), "--run-id", "run-1", "--kind", "audit", "--lines", "1"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "options-monitor logs" in out
    assert "run: run-1" in out
    assert "audit_events.jsonl exists=yes lines=1" in out
    assert '{"message":"second"}' in out
    assert '{"message":"first"}' not in out


def test_top_level_logs_json_can_tail_explicit_file(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    log_file = tmp_path / "service.log"
    log_file.write_text("one\ntwo\nthree\n", encoding="utf-8")

    rc = cli.main(["logs", "--file", str(log_file), "--lines", "2", "--json"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "logs"
    assert payload["data"]["files"][0]["tail"] == ["two", "three"]


def test_top_level_logs_missing_selected_run_returns_error(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    runs_root = tmp_path / "output_runs"
    runs_root.mkdir()

    rc = cli.main(["logs", "--runs-root", str(runs_root), "--run-id", "missing"])
    out = capsys.readouterr().out

    assert rc == 2
    assert "requested run: not found" in out


def test_top_level_setup_delegates_to_runtime_init(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    config_path = tmp_path / "config.us.json"
    data_config_path = tmp_path / "portfolio.runtime.json"

    def _init_runtime(**kwargs):
        return kwargs

    monkeypatch.setattr(cli, "init_runtime", _init_runtime)

    rc = cli.main([
        "setup",
        "--market",
        "us",
        "--futu-acc-id",
        "123456",
        "--account-label",
        "lx",
        "--config-path",
        str(config_path),
        "--data-config-path",
        str(data_config_path),
        "--symbol",
        "NVDA",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "setup"
    assert payload["data"]["market"] == "us"
    assert payload["data"]["futu_acc_id"] == "123456"
    assert payload["data"]["account_label"] == "lx"
    assert payload["data"]["config_path"] == str(config_path)
    assert payload["data"]["symbols"] == ["NVDA"]


def test_top_level_update_commands_delegate_to_service_upgrade(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    calls: list[tuple[str, dict]] = []

    def _check(**kwargs):
        calls.append(("check", kwargs))
        return {"ok": True, "status": "current"}

    def _upgrade(**kwargs):
        calls.append(("apply", kwargs))
        return {"ok": True, "status": "dry_run"}

    def _rollback(**kwargs):
        calls.append(("rollback", kwargs))
        return {"ok": True, "status": "dry_run"}

    monkeypatch.setattr(cli, "service_upgrade_check", _check)
    monkeypatch.setattr(cli, "service_upgrade", _upgrade)
    monkeypatch.setattr(cli, "service_rollback", _rollback)

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"

    assert cli.main(["update", "check", "--repo-root", str(repo), "--runtime-root", str(runtime)]) == 0
    assert _read_json_output(capsys)["tool_name"] == "update.check"

    assert cli.main([
        "update",
        "apply",
        "--repo-root",
        str(repo),
        "--runtime-root",
        str(runtime),
        "--target-version",
        "1.2.70",
    ]) == 0
    assert _read_json_output(capsys)["tool_name"] == "update.apply"

    assert cli.main([
        "update",
        "rollback",
        "--repo-root",
        str(repo),
        "--runtime-root",
        str(runtime),
        "--to-version",
        "1.2.69",
    ]) == 0
    assert _read_json_output(capsys)["tool_name"] == "update.rollback"

    assert calls[0] == ("check", {"repo_root": str(repo), "runtime_root": str(runtime), "remote_name": "origin"})
    assert calls[1][0] == "apply"
    assert calls[1][1]["repo_root"] == str(repo)
    assert calls[1][1]["runtime_root"] == str(runtime)
    assert calls[1][1]["target_version"] == "1.2.70"
    assert calls[1][1]["confirm"] is False
    assert calls[2][0] == "rollback"
    assert calls[2][1]["to_version"] == "1.2.69"
    assert calls[2][1]["confirm"] is False


def test_config_get_and_set_preview_then_apply(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    cfg = {
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 7,
                    "max_dte": 45,
                    "max_strike": 100,
                },
            }
        ],
        "runtime": {"prefetch": {"max_workers": 2}},
    }
    path = tmp_path / "config.us.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert cli.main([
        "config",
        "get",
        "--config-path",
        str(path),
        "--key",
        "runtime.prefetch.max_workers",
    ]) == 0
    payload = _read_json_output(capsys)
    assert payload["tool_name"] == "config.get"
    assert payload["data"]["value"] == 2

    assert cli.main([
        "config",
        "set",
        "--config-path",
        str(path),
        "--key",
        "runtime.prefetch.max_workers",
        "--json-value",
        "4",
    ]) == 0
    payload = _read_json_output(capsys)
    assert payload["tool_name"] == "config.set"
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["applied"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["runtime"]["prefetch"]["max_workers"] == 2

    assert cli.main([
        "config",
        "set",
        "--config-path",
        str(path),
        "--key",
        "runtime.prefetch.max_workers",
        "--json-value",
        "4",
        "--apply",
        "--confirm",
        "--no-backup",
    ]) == 0
    payload = _read_json_output(capsys)
    assert payload["data"]["applied"] is True
    assert payload["data"]["dry_run"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["runtime"]["prefetch"]["max_workers"] == 4
