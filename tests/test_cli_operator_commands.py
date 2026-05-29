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
        "candidate_report_dir": None,
        "candidate_paths": None,
        "candidate_reject_log_paths": None,
        "candidate_trace_paths": None,
        "candidate_evidence_min_sample": None,
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
        "candidate_report_dir": None,
        "candidate_paths": None,
        "candidate_reject_log_paths": None,
        "candidate_trace_paths": None,
        "candidate_evidence_min_sample": None,
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
        "candidate_report_dir": None,
        "candidate_paths": None,
        "candidate_reject_log_paths": None,
        "candidate_trace_paths": None,
        "candidate_evidence_min_sample": None,
    }]
    assert bootstrap_calls == [{
        "repo_root": cli.repo_base(),
        "env_file": str(env_file),
        "include_local_env_file": True,
    }]


def test_top_level_doctor_forwards_candidate_evidence_diagnostics(monkeypatch, capsys) -> None:
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
            "--candidate-report-dir",
            "output_shared/reports",
            "--candidate-path",
            "candidate.csv",
            "--candidate-reject-log-path",
            "reject.csv",
            "--candidate-trace-path",
            "trace.jsonl",
            "--candidate-evidence-min-sample",
            "10",
        ]
    )
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "doctor"
    assert calls[0]["candidate_report_dir"] == "output_shared/reports"
    assert calls[0]["candidate_paths"] == ["candidate.csv"]
    assert calls[0]["candidate_reject_log_paths"] == ["reject.csv"]
    assert calls[0]["candidate_trace_paths"] == ["trace.jsonl"]
    assert calls[0]["candidate_evidence_min_sample"] == 10


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


def test_assistant_model_catalog_command_renders_provider_catalog(capsys) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main(["assistant", "model", "catalog"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "assistant.model.catalog"
    providers = {item["provider"]: item for item in payload["data"]["providers"]}
    assert providers["deepseek"]["api_kind"] == "chat_completions"
    assert providers["deepseek"]["default_api_key_env"] == "DEEPSEEK_API_KEY"
    assert providers["openai"]["api_kind"] == "responses"


def test_assistant_model_list_text_does_not_print_credential_env_name(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.main as cli

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
accounts:
  lx:
    type: external_holdings
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
assistant:
  mode: llm_router
  active_model: openai-default
  models:
    openai-default:
      provider: openai
      model: gpt-5.2
      api_key_env: OM_LLM_API_KEY
""",
        encoding="utf-8",
    )

    rc = cli.main(["assistant", "model", "list", "--config-yaml", str(config_path), "--format", "text"])
    text = capsys.readouterr().out

    assert rc == 0
    assert "openai-default" in text
    assert "credential_configured=False" in text
    assert "OM_LLM_API_KEY" not in text
    assert "api_key_env" not in text


def test_assistant_model_add_dry_run_does_not_write_config(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.main as cli

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
accounts:
  lx:
    type: external_holdings
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
assistant:
  mode: deterministic
""",
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")

    rc = cli.main([
        "assistant",
        "model",
        "add",
        "deepseek-default",
        "--config-yaml",
        str(config_path),
        "--provider",
        "deepseek",
        "--model",
        "deepseek-chat",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "assistant.model.add"
    data = payload["data"]
    assert data["dry_run"] is True
    assert data["write_applied"] is False
    assert data["profile"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert config_path.read_text(encoding="utf-8") == before


def test_assistant_model_use_apply_switches_active_model_and_writes_backup(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.main as cli

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
accounts:
  lx:
    type: external_holdings
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
assistant:
  mode: llm_router
  active_model: openai-default
  models:
    openai-default:
      provider: openai
      model: gpt-5.2
      api_key_env: OM_LLM_API_KEY
    deepseek-default:
      provider: deepseek
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
""",
        encoding="utf-8",
    )

    rc = cli.main([
        "assistant",
        "model",
        "use",
        "deepseek-default",
        "--config-yaml",
        str(config_path),
        "--apply",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "assistant.model.use"
    data = payload["data"]
    assert data["dry_run"] is False
    assert data["write_applied"] is True
    assert data["backup_path"]
    assert Path(data["backup_path"]).exists()
    updated = config_path.read_text(encoding="utf-8")
    assert "active_model: deepseek-default" in updated
    assert "rebuild_hint" in data


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
    assert "/record-open" in text
    assert "/record-close" in text
    assert "/confirm trade|symbol|upgrade|model" in text


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


def test_strategy_lab_is_not_a_public_cli_surface() -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit):
        cli.main(["strategy-lab", "current"])
    with pytest.raises(SystemExit):
        cli.main(["strategy", "current"])


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


def test_config_build_rejects_legacy_source(capsys) -> None:
    import src.interfaces.cli.main as cli

    rc = cli.main([
        "config",
        "build",
        "--source",
        "legacy",
        "--market",
        "us",
        "--dry-run",
    ])
    payload = _read_json_output(capsys)

    assert rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INPUT_ERROR"
    assert payload["error"]["details"]["allowed"] == ["yaml"]


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


def test_config_build_removes_legacy_json_flags(capsys) -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit) as exc:
        cli.main([
            "config",
            "build",
            "--market",
            "us",
            "--user-config",
            "configs/examples/user.example.us.json",
            "--dry-run",
        ])

    assert exc.value.code == 2
    assert "unrecognized arguments: --user-config" in capsys.readouterr().err


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


@pytest.mark.parametrize("argv", [["config", "build", "--help"], ["config", "explain", "--help"]])
def test_config_authoring_help_hides_legacy_flags(argv: list[str], capsys) -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert "--source {yaml}" in out
    assert "--source legacy" not in out
    assert "--common-user-config" not in out
    assert "--no-common-user-config" not in out
    assert "--user-config" not in out


def test_setup_init_command_is_removed(capsys) -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "init", "--market", "us", "--futu-acc-id", "12345678"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_service_render_requires_yaml_authoring_source(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = tmp_path / "runtime"

    with pytest.raises(SystemExit) as exc:
        cli.main([
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

    assert exc.value.code == 2
    assert "the following arguments are required: --config-yaml" in capsys.readouterr().err


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


def test_config_get_reads_runtime_snapshot(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    cfg = {
        "_generated": {
            "schema_version": "1.0",
            "generator": "options-monitor",
            "source_format": "yaml",
            "market": "us",
        },
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

    assert json.loads(path.read_text(encoding="utf-8"))["runtime"]["prefetch"]["max_workers"] == 2


def test_config_set_command_is_removed(capsys) -> None:
    import src.interfaces.cli.main as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "set", "--config-path", "config.us.json", "--key", "runtime.prefetch.max_workers", "--json-value", "4"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
