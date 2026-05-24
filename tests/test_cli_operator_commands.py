from __future__ import annotations

import os
import json
from pathlib import Path

import pytest


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
        "audit_db": None,
        "profile_path": None,
        "env_file": None,
        "include_service_status": False,
        "strategy_report_dir": None,
        "strategy_candidate_paths": None,
        "strategy_reject_log_paths": None,
        "strategy_trace_paths": None,
        "strategy_outcome_paths": None,
        "strategy_evidence_min_sample": None,
    }]


def test_top_level_healthcheck_passes_inbound_diagnostics_args(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _healthcheck(**kwargs):
        calls.append(kwargs)
        return {"tool_name": "healthcheck", "ok": True, "data": {"status": "pass"}}

    monkeypatch.setattr(cli, "run_healthcheck", _healthcheck)

    rc = cli.main(
        [
            "healthcheck",
            "--config-path",
            "config.us.json",
            "--audit-db",
            "inbound.sqlite3",
            "--profile-path",
            "service.profile.json",
            "--include-service-status",
        ]
    )
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "healthcheck"
    assert calls == [{
        "config_key": None,
        "config_path": "config.us.json",
        "accounts": None,
        "opend_telnet_host": None,
        "opend_telnet_port": None,
        "audit_db": "inbound.sqlite3",
        "profile_path": "service.profile.json",
        "env_file": None,
        "include_service_status": True,
        "strategy_report_dir": None,
        "strategy_candidate_paths": None,
        "strategy_reject_log_paths": None,
        "strategy_trace_paths": None,
        "strategy_outcome_paths": None,
        "strategy_evidence_min_sample": None,
    }]


def test_top_level_healthcheck_forwards_env_file(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_FEISHU_BOT_APP_ID=cli_1\n", encoding="utf-8")
    bootstrap_calls: list[dict] = []
    calls: list[dict] = []

    def _bootstrap_process_env(**kwargs):
        bootstrap_calls.append(kwargs)

    def _healthcheck(**kwargs):
        calls.append(kwargs)
        return {"tool_name": "healthcheck", "ok": True, "data": {"status": "pass"}}

    monkeypatch.setattr(cli, "bootstrap_process_env", _bootstrap_process_env)
    monkeypatch.setattr(cli, "run_healthcheck", _healthcheck)

    rc = cli.main(["healthcheck", "--config-key", "us", "--env-file", str(env_file)])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "healthcheck"
    assert calls == [{
        "config_key": "us",
        "config_path": None,
        "accounts": None,
        "opend_telnet_host": None,
        "opend_telnet_port": None,
        "audit_db": None,
        "profile_path": None,
        "env_file": str(env_file),
        "include_service_status": False,
        "strategy_report_dir": None,
        "strategy_candidate_paths": None,
        "strategy_reject_log_paths": None,
        "strategy_trace_paths": None,
        "strategy_outcome_paths": None,
        "strategy_evidence_min_sample": None,
    }]
    assert bootstrap_calls == [{
        "repo_root": cli.repo_base(),
        "env_file": str(env_file),
        "include_local_env_file": True,
    }]


def test_top_level_doctor_forwards_strategy_evidence_diagnostics(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _healthcheck(**kwargs):
        calls.append(kwargs)
        return {"tool_name": "healthcheck", "ok": True, "data": {"status": "pass"}}

    monkeypatch.setattr(cli, "run_healthcheck", _healthcheck)

    rc = cli.main(
        [
            "doctor",
            "--config-key",
            "us",
            "--strategy-report-dir",
            "output_shared/reports",
            "--strategy-candidate-path",
            "candidate.csv",
            "--strategy-reject-log-path",
            "reject.csv",
            "--strategy-trace-path",
            "trace.jsonl",
            "--strategy-outcome-path",
            "outcome.csv",
            "--strategy-evidence-min-sample",
            "10",
        ]
    )
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "doctor"
    assert calls[0]["strategy_report_dir"] == "output_shared/reports"
    assert calls[0]["strategy_candidate_paths"] == ["candidate.csv"]
    assert calls[0]["strategy_reject_log_paths"] == ["reject.csv"]
    assert calls[0]["strategy_trace_paths"] == ["trace.jsonl"]
    assert calls[0]["strategy_outcome_paths"] == ["outcome.csv"]
    assert calls[0]["strategy_evidence_min_sample"] == 10


def test_support_bundle_command_forwards_diagnostic_args(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _support_bundle_response(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": "1.0",
            "tool_name": "support.bundle",
            "ok": True,
            "data": {"bundle_path": "/tmp/options-monitor-support.json"},
            "warnings": [],
            "error": None,
            "meta": {},
        }

    monkeypatch.setattr(cli, "support_bundle_response", _support_bundle_response)

    rc = cli.main([
        "support",
        "bundle",
        "--config-key",
        "us",
        "--accounts",
        "lx",
        "sy",
        "--profile-path",
        "service.profile.json",
        "--env-file",
        "options-monitor.env",
        "--no-local-env-file",
        "--include-healthcheck",
        "--runtime-root",
        "/var/lib/options-monitor",
        "--output-dir",
        "/tmp/support",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "support.bundle"
    assert calls == [{
        "repo_root": cli.repo_base(),
        "config_key": "us",
        "config_path": None,
        "accounts": ["lx", "sy"],
        "profile_path": "service.profile.json",
        "env_file": "options-monitor.env",
        "include_local_env_file": False,
        "include_healthcheck": True,
        "output_dir": "/tmp/support",
        "runtime_root": "/var/lib/options-monitor",
    }]


def test_assistant_llm_check_command_forwards_diagnostic_args(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _check_llm_translator(**kwargs):
        calls.append(kwargs)
        return {"summary": {"ok": True, "status": "ready"}, "checks": []}

    monkeypatch.setattr(cli, "check_llm_translator", _check_llm_translator)

    rc = cli.main([
        "assistant",
        "llm-check",
        "--assistant-config",
        "config.assistant.json",
        "--env-file",
        "options-monitor.env",
        "--no-local-env-file",
        "--live",
        "--text",
        "状态",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "assistant.llm_check"
    assert payload["ok"] is True
    assert calls == [{
        "repo_root": cli.repo_base(),
        "config_path": "config.assistant.json",
        "env_file": "options-monitor.env",
        "include_local_env_file": False,
        "live": True,
        "live_text": "状态",
    }]


def test_no_local_env_file_flag_prevents_process_env_bootstrap() -> None:
    import src.interfaces.cli.main as cli

    assert cli._should_bootstrap_process_env(["assistant", "llm-check"]) is True
    assert cli._should_bootstrap_process_env(["healthcheck", "--env-file", "prod.env"]) is False
    assert cli._should_bootstrap_process_env(["assistant", "llm-check", "--no-local-env-file"]) is False
    assert cli._should_bootstrap_process_env(["support", "bundle", "--no-local-env-file"]) is False


def test_assistant_commands_command_renders_catalog(capsys) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main(["assistant", "commands"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "assistant.commands"
    assert payload["ok"] is True
    assert payload["data"]["summary"]["llm_allowed_count"] >= 1
    intents = {item["intent_name"] for item in payload["data"]["commands"]}
    assert "runtime_status" in intents
    assert "manual_trade_confirm" in intents

    rc = cli.main(["assistant", "commands", "--format", "text"])
    text = capsys.readouterr().out

    assert rc == 0
    assert "/status" in text
    assert "/confirm trade|symbol|upgrade" in text


def test_assistant_capabilities_command_renders_capability_catalog(capsys) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main(["assistant", "capabilities"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "assistant.capabilities"
    assert payload["ok"] is True
    assert payload["data"]["summary"]["capability_count"] >= payload["data"]["summary"]["slash_command_count"]
    capabilities = {item["capability_id"]: item for item in payload["data"]["capabilities"]}
    assert capabilities["runtime_status"]["llm_executable"] is True
    assert capabilities["manual_trade_open"]["llm_executable"] is False
    assert capabilities["upgrade_now"]["risk_level"] == "preview_admin"

    rc = cli.main(["assistant", "capabilities", "--format", "text"])
    text = capsys.readouterr().out

    assert rc == 0
    assert "Assistant capabilities" in text
    assert "LLM executable read-only capabilities" in text
    assert "Known capabilities not executable by LLM" in text
    assert "runtime_status (状态): risk=read_only llm_executable=true" in text
    assert "manual_trade_open (记录开仓): risk=preview_write llm_executable=false" in text
    assert "upgrade_now (立即升级): risk=preview_admin llm_executable=false" in text


def test_legacy_agent_command_alias_is_hidden_but_supported(capsys) -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["--help"])
    help_text = capsys.readouterr().out

    assert exc.value.code == 0
    assert "assistant" in help_text
    assert " agent " not in help_text

    rc = cli.main(["agent", "commands"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "assistant.commands"
    assert payload["ok"] is True


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


def test_top_level_status_forwards_env_file(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_RUNTIME_ROOT=/tmp/options-monitor\n", encoding="utf-8")
    bootstrap_calls: list[dict] = []
    calls: list[tuple[str, dict]] = []

    def _bootstrap_process_env(**kwargs):
        bootstrap_calls.append(kwargs)

    def _execute_tool(name: str, payload: dict) -> dict:
        calls.append((name, payload))
        return _runtime_status_envelope()

    monkeypatch.setattr(cli, "bootstrap_process_env", _bootstrap_process_env)
    monkeypatch.setattr(cli, "execute_tool", _execute_tool)

    rc = cli.main(["status", "--config-key", "us", "--env-file", str(env_file), "--json"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "runtime_status"
    assert calls == [("runtime_status", {"config_key": "us", "env_file": str(env_file)})]
    assert bootstrap_calls == [{
        "repo_root": cli.repo_base(),
        "env_file": str(env_file),
        "include_local_env_file": True,
    }]


def test_research_collect_forwards_remote_runtime_selection(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[tuple[str, dict]] = []

    def _execute_tool(name: str, payload: dict) -> dict:
        calls.append((name, payload))
        return {"tool_name": "research", "ok": True, "data": {"status": "ok"}}

    monkeypatch.setattr(cli, "execute_tool", _execute_tool)

    rc = cli.main([
        "research",
        "collect",
        "--config-key",
        "us",
        "--config-path",
        "/var/lib/options-monitor/config.us.json",
        "--profile-path",
        "/var/lib/options-monitor/service.profile.json",
        "--runs-root",
        "/var/lib/options-monitor/output_runs",
        "--report-dir",
        "/var/lib/options-monitor/output_shared/reports",
        "--shared-state-dir",
        "/var/lib/options-monitor/output_shared/state",
        "--accounts-root",
        "/var/lib/options-monitor/output_accounts",
        "--run-id",
        "run-1",
        "--runs-limit",
        "3",
        "--tail-limit",
        "50",
        "--max-run-age-minutes",
        "90",
        "--max-notification-chars",
        "2000",
        "--output",
        "json",
        "--no-write-outputs",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "research"
    assert calls == [
        (
            "research",
            {
                "scope": "full",
                "config_key": "us",
                "config_path": "/var/lib/options-monitor/config.us.json",
                "profile_path": "/var/lib/options-monitor/service.profile.json",
                "report_dir": "/var/lib/options-monitor/output_shared/reports",
                "shared_state_dir": "/var/lib/options-monitor/output_shared/state",
                "accounts_root": "/var/lib/options-monitor/output_accounts",
                "runs_root": "/var/lib/options-monitor/output_runs",
                "run_id": "run-1",
                "runs_limit": 3,
                "tail_limit": 50,
                "max_run_age_minutes": 90,
                "max_notification_chars": 2000,
                "output": "json",
                "include_healthcheck": False,
                "write_outputs": False,
                "confirm": False,
            },
        )
    ]


def test_strategy_lab_replay_is_not_a_public_cli_surface() -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit):
        cli.main(["strategy-lab", "replay"])


def test_strategy_lab_historical_fetch_forwards_payload(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _fetch(payload: dict, *, base):
        calls.append(dict(payload))
        return (
            {
                "schema_version": "strategy_lab_historical_fetch.v1",
                "provider": "futu",
                "dry_run": False,
                "request": {
                    "symbols": ["NVDA", "0700.HK"],
                    "start_date": "2026-05-01",
                    "end_date": "2026-05-03",
                    "timeframe": "1d",
                },
                "output": {"snapshot_path": "output_shared/strategy_lab/historical_data/futu-abc.json"},
            },
            [],
            {"base": str(base)},
        )

    monkeypatch.setattr(cli, "fetch_historical_data_tool", _fetch)

    rc = cli.main([
        "strategy-lab",
        "historical",
        "fetch",
        "--symbols",
        "NVDA,0700.HK",
        "--start-date",
        "2026-05-01",
        "--end-date",
        "2026-05-03",
        "--config-key",
        "us",
        "--host",
        "127.0.0.9",
        "--port",
        "11119",
        "--max-count",
        "100",
        "--max-pages",
        "2",
        "--confirm",
        "--json",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "strategy-lab.historical.fetch"
    assert payload["data"]["output"]["snapshot_path"].endswith("futu-abc.json")
    assert calls == [
        {
            "provider": "futu",
            "symbols": "NVDA,0700.HK",
            "start_date": "2026-05-01",
            "end_date": "2026-05-03",
            "asset_type": "underlying",
            "timeframe": "1d",
            "adjusted": False,
            "config_key": "us",
            "host": "127.0.0.9",
            "port": 11119,
            "max_count": 100,
            "max_pages": 2,
            "no_retry": False,
            "dry_run": False,
            "confirm": True,
        }
    ]


def test_strategy_lab_dataset_collect_forwards_payload(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _collect(payload: dict, *, base):
        calls.append(dict(payload))
        return (
            {
                "schema_version": "strategy_lab_dataset_collect.v1",
                "dry_run": False,
                "dataset": {
                    "dataset_id": "ds-1",
                    "scope": {"market": "us", "account": "sy", "strategy_type": "sell_put"},
                    "summary": {"candidate_count": 5, "outcome_count": 5, "reject_count": 1, "trace_count": 1},
                },
                "output": {"dataset_path": "output_shared/strategy_lab/datasets/ds-1.json"},
            },
            [],
            {"base": str(base)},
        )

    monkeypatch.setattr(cli, "strategy_lab_dataset_collect_tool", _collect)

    rc = cli.main([
        "strategy-lab",
        "dataset",
        "collect",
        "--config-key",
        "us",
        "--account",
        "sy",
        "--strategy-type",
        "sell_put",
        "--candidate-path",
        "sell_put_candidates.csv",
        "--outcome-path",
        "strategy_replay.csv",
        "--sample-limit",
        "3",
        "--confirm",
        "--json",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "strategy-lab.dataset.collect"
    assert payload["data"]["dataset"]["dataset_id"] == "ds-1"
    assert calls == [
        {
            "config_key": "us",
            "account": "sy",
            "strategy_type": "sell_put",
            "candidate_paths": ["sell_put_candidates.csv"],
            "outcome_paths": ["strategy_replay.csv"],
            "sample_limit": 3,
            "dry_run": False,
            "confirm": True,
            "yes": False,
        }
    ]


def test_strategy_lab_experiment_forwards_payload(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _experiment(payload: dict, *, base):
        calls.append(dict(payload))
        return (
            {
                "schema_version": "strategy_lab_experiment_run.v1",
                "dry_run": True,
                "dataset": {"dataset_id": "ds-1"},
                "result": {
                    "experiment_id": "exp-1",
                    "dataset_id": "ds-1",
                    "status": "evaluable",
                    "recommendation": {"recommendation": "watch", "reason": "low_sample"},
                    "preflight": {"sample": {"candidate_count": 5, "outcome_count": 5, "reject_count": 1, "trace_count": 1}},
                },
                "output": {"result_path": None, "report_path": None, "current_path": "output_shared/state/current/strategy_lab.current.json"},
            },
            [],
            {"base": str(base)},
        )

    monkeypatch.setattr(cli, "strategy_lab_experiment_tool", _experiment)

    rc = cli.main([
        "strategy-lab",
        "experiment",
        "--dataset-id",
        "ds-1",
        "--candidate-grid-path",
        "grid.json",
        "--candidate-params-json",
        '{"max_candidates": 5}',
        "--min-candidate-sample",
        "5",
        "--json",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "strategy-lab.experiment"
    assert payload["data"]["result"]["recommendation"]["recommendation"] == "watch"
    assert calls == [
        {
            "dataset_id": "ds-1",
            "candidate_grid_path": "grid.json",
            "candidate_params": {"max_candidates": 5},
            "min_candidate_sample": 5,
            "dry_run": False,
            "confirm": False,
            "yes": False,
        }
    ]


def test_strategy_lab_current_forwards_payload(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _current(payload: dict, *, base):
        calls.append(dict(payload))
        return (
            {"schema_version": "strategy_lab_current_read.v1", "current": {"exists": False, "current_path": "output_shared/state/current/strategy_lab.current.json"}},
            [],
            {"base": str(base)},
        )

    monkeypatch.setattr(cli, "strategy_lab_current_tool", _current)

    rc = cli.main(["strategy-lab", "current", "--runtime-root", "/tmp/om-runtime", "--json"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "strategy-lab.current"
    assert payload["data"]["current"]["exists"] is False
    assert calls == [{"runtime_root": "/tmp/om-runtime"}]


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


def test_top_level_setup_requires_current_subcommand(capsys) -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "--market", "us", "--futu-acc-id", "123456"])

    assert exc.value.code == 2
    assert "setup" in capsys.readouterr().err


def test_legacy_config_build_emits_deprecation_warning(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main([
        "config",
        "build",
        "--source",
        "legacy",
        "--market",
        "us",
        "--common-user-config",
        "configs/examples/user.common.example.json",
        "--user-config",
        "configs/examples/user.example.us.json",
        "--output",
        str(tmp_path / "config.us.json"),
        "--dry-run",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert cli.LEGACY_CONFIG_AUTHORING_DEPRECATION_WARNING in payload["warnings"]


def test_config_build_defaults_to_yaml_source(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        """\
accounts:
  lx:
    type: futu
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
  hk:
    accounts: [lx]
    symbols: ["0700.HK"]
""",
        encoding="utf-8",
    )

    rc = cli.main([
        "config",
        "build",
        "--market",
        "us",
        "--config-yaml",
        str(config_yaml),
        "--output",
        str(tmp_path / "config.us.json"),
        "--dry-run",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["source_format"] == "yaml"
    assert payload["dry_run"] is True
    assert "warnings" not in payload


def test_config_build_rejects_legacy_flags_without_legacy_source(capsys) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main([
        "config",
        "build",
        "--market",
        "us",
        "--user-config",
        "configs/examples/user.example.us.json",
        "--dry-run",
    ])
    payload = _read_json_output(capsys)

    assert rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INPUT_ERROR"
    assert payload["error"]["details"]["flags"] == ["--user-config"]


def test_config_validate_rejects_runtime_flags_with_yaml_source(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        """\
accounts:
  lx:
    type: futu
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
        encoding="utf-8",
    )

    rc = cli.main([
        "config",
        "validate",
        "--source",
        "yaml",
        "--market",
        "us",
        "--config-yaml",
        str(config_yaml),
        "--config-path",
        "config.us.json",
    ])
    payload = _read_json_output(capsys)

    assert rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INPUT_ERROR"
    assert payload["error"]["details"]["flags"] == ["--config-path"]


def test_config_validate_rejects_yaml_flag_with_runtime_source(capsys) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main([
        "config",
        "validate",
        "--source",
        "runtime",
        "--config-yaml",
        "config.yaml",
    ])
    payload = _read_json_output(capsys)

    assert rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INPUT_ERROR"
    assert payload["error"]["details"]["flags"] == ["--config-yaml"]


def test_config_validate_defaults_to_runtime_source(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _validate_runtime_config(**kwargs):
        calls.append(kwargs)
        return {"tool_name": "config.validate", "ok": True, "data": {"status": "pass"}}

    monkeypatch.setattr(cli, "_validate_runtime_config", _validate_runtime_config)

    rc = cli.main([
        "config",
        "validate",
        "--config-path",
        "config.us.json",
        "--market",
        "us",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert calls == [{"config_key": None, "config_path": "config.us.json", "market": "us"}]


def test_setup_init_emits_deprecation_warning(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _init_runtime(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "config_path": "config.us.json"}

    monkeypatch.setattr(cli, "init_runtime", _init_runtime)

    rc = cli.main(["setup", "init", "--market", "us", "--futu-acc-id", "12345678"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "setup.init"
    assert payload["warnings"] == [cli.SETUP_INIT_DEPRECATION_WARNING]
    assert calls[0]["market"] == "us"


def test_service_render_warns_without_yaml_authoring_source(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = tmp_path / "runtime"

    rc = cli.main([
        "service",
        "render",
        "--target",
        "systemd",
        "--repo-root",
        str(repo),
        "--runtime-root",
        str(runtime),
        "--markets",
        "us",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "service.render"
    assert payload["warnings"] == [cli.SERVICE_RENDER_LEGACY_AUTHORING_WARNING]


def test_init_runtime_command_is_removed(capsys) -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["init", "runtime", "--market", "us", "--futu-acc-id", "123456"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


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

    assert calls[0] == ("check", {"repo_root": str(repo), "runtime_root": str(runtime), "cache_root": None, "remote_name": "origin"})
    assert calls[1][0] == "apply"
    assert calls[1][1]["repo_root"] == str(repo)
    assert calls[1][1]["runtime_root"] == str(runtime)
    assert calls[1][1]["target_version"] == "1.2.70"
    assert calls[1][1]["confirm"] is False
    assert calls[2][0] == "rollback"
    assert calls[2][1]["to_version"] == "1.2.69"
    assert calls[2][1]["confirm"] is False


def test_service_upgrade_compat_commands_are_removed(capsys) -> None:
    import src.interfaces.cli.main as cli

    for command in ("upgrade-check", "upgrade", "rollback"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["service", command])
        assert exc.value.code == 2
        assert "invalid choice" in capsys.readouterr().err


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
    assert payload["warnings"] == [cli.RUNTIME_CONFIG_SET_DEPRECATION_WARNING]
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
        "--no-backup",
    ]) == 0
    payload = _read_json_output(capsys)
    assert payload["warnings"] == [cli.RUNTIME_CONFIG_SET_DEPRECATION_WARNING]
    assert payload["data"]["applied"] is True
    assert payload["data"]["dry_run"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["runtime"]["prefetch"]["max_workers"] == 4
