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


def test_multi_tick_and_webui_use_application_facades() -> None:
    multi_src = (ROOT / "scripts" / "send_if_needed_multi.py").read_text(encoding="utf-8")
    webui_src = (ROOT / "scripts" / "webui" / "server.py").read_text(encoding="utf-8")
    service_src = (ROOT / "scripts" / "infra" / "service.py").read_text(encoding="utf-8")
    om_src = (ROOT / "om").read_text(encoding="utf-8")
    agent_src = (ROOT / "om-agent").read_text(encoding="utf-8")
    send_if_needed_src = (ROOT / "scripts" / "send_if_needed.py").read_text(encoding="utf-8")
    multi_tick_main_src = (ROOT / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    multi_account_tick_src = (ROOT / "src" / "application" / "multi_account_tick.py").read_text(encoding="utf-8")
    webui_interface_src = (ROOT / "src" / "interfaces" / "webui" / "server.py").read_text(encoding="utf-8")
    multi_tick_scheduler_src = (ROOT / "src" / "application" / "multi_tick_scheduler.py").read_text(encoding="utf-8")
    multi_tick_finalization_src = (ROOT / "src" / "application" / "multi_tick_finalization.py").read_text(encoding="utf-8")
    cron_runtime_src = (ROOT / "src" / "application" / "cron_runtime.py").read_text(encoding="utf-8")
    agent_tools_src = (ROOT / "src" / "application" / "agent_tools.py").read_text(encoding="utf-8")
    tool_execution_src = (ROOT / "src" / "application" / "tool_execution.py").read_text(encoding="utf-8")

    assert "src.application.multi_account_tick" in multi_src
    assert "run_tick" in multi_src
    assert "src.interfaces.webui" in webui_src
    assert "from src.application.account_management import add_account, edit_account, remove_account" in webui_interface_src
    assert "from src.application.tool_execution import build_tool_manifest, execute_tool" in webui_interface_src
    assert "src.interfaces.cli.main" in service_src
    assert "src.interfaces.cli.main" in om_src
    assert "src.interfaces.agent.cli" in agent_src
    assert "from src.application.multi_account_tick import current_run_id, run_tick" in send_if_needed_src
    assert "run_pipeline_script" not in send_if_needed_src
    assert "src.application.multi_account_tick" in multi_tick_main_src
    assert "scripts.multi_tick.main" not in multi_account_tick_src
    assert "run_scheduler_flow" in multi_account_tick_src
    assert "build_multi_tick_scheduler_decision" in multi_tick_scheduler_src
    assert "build_multi_tick_account_scheduler_view" in multi_tick_scheduler_src
    assert "apply_notify_results_to_tick_metrics" in multi_account_tick_src
    assert "build_shared_last_run_meta" in multi_tick_finalization_src
    assert "build_run_end_payload" in multi_tick_finalization_src
    assert "def build_notify_summary(" in cron_runtime_src
    assert "scripts.agent_plugin.main" not in agent_tools_src
    assert "scripts.agent_plugin.tools" not in tool_execution_src
    assert "src.application.agent_tool_registry" in agent_tools_src
    assert "src.application.agent_tool_registry" in tool_execution_src


def test_multi_tick_script_path_help_bootstraps_repo_root() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/multi_tick/main.py",
            "--help",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Multi-account tick" in proc.stdout
    assert "--config" in proc.stdout


def test_unified_cli_validate_command_works_with_example_config() -> None:
    proc = subprocess.run(
        [
            str((ROOT / ".venv" / "bin" / "python").resolve()),
            "-m",
            "src.interfaces.cli.main",
            "config",
            "validate",
            "--config-path",
            str((ROOT / "configs" / "examples" / "config.example.us.json").resolve()),
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
