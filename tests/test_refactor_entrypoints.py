from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shell_entrypoints_target_src_interfaces() -> None:
    om_src = (ROOT / "om").read_text(encoding="utf-8")
    agent_src = (ROOT / "om-agent").read_text(encoding="utf-8")

    assert "src.interfaces.cli.main" in om_src
    assert "src.interfaces.agent.cli" in agent_src


def test_runtime_entrypoints_use_application_facades() -> None:
    service_src = (ROOT / "src" / "infrastructure" / "external_services.py").read_text(encoding="utf-8")
    agent_runtime_src = (ROOT / "src" / "application" / "agent_tool_runtime.py").read_text(encoding="utf-8")
    pipeline_runtime_src = (ROOT / "src" / "application" / "pipeline_runtime.py").read_text(encoding="utf-8")
    cli_src = (ROOT / "src" / "interfaces" / "cli" / "main.py").read_text(encoding="utf-8")
    om_src = (ROOT / "om").read_text(encoding="utf-8")
    agent_src = (ROOT / "om-agent").read_text(encoding="utf-8")
    multi_account_tick_src = (ROOT / "src" / "application" / "multi_account_tick.py").read_text(encoding="utf-8")
    tick_notification_flow_src = (ROOT / "src" / "application" / "tick_notification_flow.py").read_text(encoding="utf-8")
    multi_tick_scheduler_src = (ROOT / "src" / "application" / "multi_tick_scheduler.py").read_text(encoding="utf-8")
    tick_scheduler_context_src = (ROOT / "src" / "application" / "tick_scheduler_context.py").read_text(encoding="utf-8")
    multi_tick_finalization_src = (ROOT / "src" / "application" / "multi_tick_finalization.py").read_text(encoding="utf-8")
    cron_runtime_src = (ROOT / "src" / "application" / "cron_runtime.py").read_text(encoding="utf-8")
    tool_execution_src = (ROOT / "src" / "application" / "tool_execution.py").read_text(encoding="utf-8")
    healthcheck_src = (ROOT / "src" / "application" / "healthcheck.py").read_text(encoding="utf-8")
    healthcheck_runner_src = (ROOT / "src" / "application" / "healthcheck_runner.py").read_text(encoding="utf-8")
    required_data_prefetch_src = (ROOT / "src" / "application" / "multi_tick" / "required_data_prefetch.py").read_text(encoding="utf-8")
    prefetch_coordinator_src = (ROOT / "src" / "application" / "multi_tick" / "prefetch_coordinator.py").read_text(encoding="utf-8")
    scan_pipeline_src = (ROOT / "src" / "application" / "scan_pipeline.py").read_text(encoding="utf-8")
    notification_pipeline_src = (ROOT / "src" / "application" / "notification_pipeline.py").read_text(encoding="utf-8")
    close_advice_pipeline_src = (ROOT / "src" / "application" / "close_advice_pipeline.py").read_text(encoding="utf-8")

    assert not (ROOT / "scripts" / "send_if_needed.py").exists()
    assert not (ROOT / "scripts" / "send_if_needed_multi.py").exists()
    assert not (ROOT / "scripts" / "option_positions.py").exists()
    assert not (ROOT / "run_watchlist.sh").exists()
    assert not (ROOT / "scripts" / "models.py").exists()
    assert not (ROOT / "scripts" / "daily_health_summary.py").exists()
    assert not (ROOT / "scripts" / "doctor_opend_required_fields.py").exists()
    assert not (ROOT / "scripts" / "doctor_futu.py").exists()
    assert not (ROOT / "scripts" / "append_cash_summary.py").exists()
    assert not (ROOT / "scripts" / "auto_close_expired_positions.py").exists()
    assert not (ROOT / "scripts" / "auto_deploy_from_main.py").exists()
    assert not (ROOT / "scripts" / "diagnose_opend_option_chain.py").exists()
    assert not (ROOT / "scripts" / "deploy_safe.sh").exists()
    assert not (ROOT / "scripts" / "deploy_to_prod.py").exists()
    assert not (ROOT / "scripts" / "deploy_status.py").exists()
    assert not (ROOT / "scripts" / "healthcheck.py").exists()
    assert not (ROOT / "scripts" / "healthcheck_and_notify.py").exists()
    assert not (ROOT / "scripts" / "opend_watchdog.py").exists()
    assert not (ROOT / "scripts" / "opend_watchdog_loop.py").exists()
    assert not (ROOT / "scripts" / "policy_check.py").exists()
    assert not (ROOT / "scripts" / "publish_to_prod.sh").exists()
    assert not (ROOT / "scripts" / "retention_reports.py").exists()
    assert not (ROOT / "src" / "application" / "deploy_observability.py").exists()
    assert not (ROOT / "scripts" / "ssh_selfcheck.sh").exists()
    assert not (ROOT / "scripts" / "tools" / "doctor_opend_telnet.py").exists()
    assert not (ROOT / "scripts" / "tools" / "doctor_required_data_schema.py").exists()
    assert not (ROOT / "scripts" / "tools" / "sell_put_cash_and_notify.py").exists()
    assert not (ROOT / "scripts" / "tools" / "snip_sell_put_headroom.py").exists()
    assert not (ROOT / "run_webui.sh").exists()
    assert not (ROOT / "scripts" / "webui").exists()
    assert not (ROOT / "src" / "interfaces" / "webui").exists()
    assert "src.interfaces.cli.main" in service_src
    assert "scripts/opend_watchdog.py" not in service_src
    assert "scripts/doctor_futu.py" not in agent_runtime_src
    assert "scripts/append_cash_summary.py" not in pipeline_runtime_src
    assert "from src.application.futu_doctor import run_futu_doctor_checks" in agent_runtime_src
    assert "from src.application.cash_summary_footer import append_cash_summary_footer" in pipeline_runtime_src
    assert "from src.infrastructure.opend_watchdog import run_watchdog_check" in service_src
    assert "src.interfaces.cli.main" in om_src
    assert "src.interfaces.agent.cli" in agent_src
    assert "from src.application.multi_account_tick import run_tick" in cli_src
    assert "return int(run_tick(tick_argv))" in cli_src
    assert "src.interfaces.cli.option_positions" in cli_src
    assert "src.application.multi_tick.main" not in multi_account_tick_src
    assert not (ROOT / "scripts" / "multi_tick" / "main.py").exists()
    assert not (ROOT / "scripts" / "infra" / "service.py").exists()
    assert "run_scheduler_flow" in tick_scheduler_context_src
    assert "build_multi_tick_scheduler_decision" in multi_tick_scheduler_src
    assert "build_multi_tick_account_scheduler_view" in multi_tick_scheduler_src
    assert "apply_notify_results_to_tick_metrics" in tick_notification_flow_src
    assert "build_shared_last_run_meta" in multi_tick_finalization_src
    assert "build_run_end_payload" in multi_tick_finalization_src
    assert "def build_notify_summary(" in cron_runtime_src
    assert not (ROOT / "src" / "application" / "agent_tools.py").exists()
    assert not (ROOT / "scripts" / "agent_plugin").exists()
    assert "from src.application.tool_execution import execute_tool" in healthcheck_src
    assert "get_tenant_access_token" in healthcheck_runner_src
    assert "run_scheduler" in healthcheck_runner_src
    assert "from src.application.multi_tick.prefetch_coordinator import PrefetchCoordinator" in required_data_prefetch_src
    assert "ThreadPoolExecutor" not in required_data_prefetch_src
    assert "ThreadPoolExecutor" in prefetch_coordinator_src
    assert "from src.application.tool_execution import execute_tool" in scan_pipeline_src
    assert "from src.application.tool_execution import execute_tool" in notification_pipeline_src
    assert "from src.application.tool_execution import execute_tool" in close_advice_pipeline_src
    assert "scripts.agent_plugin.tools" not in tool_execution_src
    assert "src.application.agent_tool_registry" in tool_execution_src


def test_unified_tick_help_works() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.interfaces.cli.main",
            "run",
            "tick",
            "--help",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "run tick" in proc.stdout
    assert "--config" in proc.stdout


def test_unified_cli_validate_command_works_with_example_config(example_config_path: Path) -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.cli.main",
            "config",
            "validate",
            "--config-path",
            str(example_config_path),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True


def test_agent_interface_spec_outputs_manifest() -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.agent.cli",
            "spec",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["name"] == "options-monitor-local-tools"
    assert any(str(item.get("name")) == "healthcheck" for item in payload.get("tools", []))


def test_unified_cli_scan_pipeline_command_exposes_canonical_flags() -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.cli.main",
            "scan-pipeline",
            "--help",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--report-dir" in proc.stdout
    assert "--shared-context-dir" in proc.stdout
    assert "--shared-scan-dir" not in proc.stdout
    assert "--reuse-shared-scan" not in proc.stdout


def test_unified_cli_option_positions_sync_feishu_command_is_removed() -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.cli.main",
            "option-positions",
            "sync-feishu",
            "--help",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "invalid choice" in proc.stderr


def test_unified_cli_option_positions_management_command_exists_without_legacy_market_alias() -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.cli.main",
            "option-positions",
            "list",
            "--help",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--broker" in proc.stdout
    assert "--market" not in proc.stdout


def test_unified_cli_option_positions_report_command_exists_without_legacy_market_alias() -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.cli.main",
            "option-positions",
            "report",
            "monthly-income",
            "--help",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--broker" in proc.stdout
    assert "--market" not in proc.stdout


def test_unified_cli_symbols_command_exists_without_legacy_script_path() -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.cli.main",
            "symbols",
            "list",
            "--help",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--format" in proc.stdout
    assert "scripts/watchlist.py" not in proc.stdout


def test_unified_cli_watchlist_command_is_removed() -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.cli.main",
            "watchlist",
            "list",
            "--help",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
