from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shell_entrypoints_work_outside_repo_cwd(tmp_path: Path) -> None:
    proc = subprocess.run(
        [str((ROOT / "om").resolve()), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "usage:" in proc.stdout

    agent_proc = subprocess.run(
        [str((ROOT / "om-agent").resolve()), "spec"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(agent_proc.stdout)
    assert payload["name"] == "options-monitor-local-tools"


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
            sys.executable,
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
            sys.executable,
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
            sys.executable,
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
            sys.executable,
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
            sys.executable,
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


def test_unified_cli_option_performance_report_is_the_public_performance_entrypoint() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.interfaces.cli.main",
            "option-performance",
            "report",
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
            sys.executable,
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
            sys.executable,
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
