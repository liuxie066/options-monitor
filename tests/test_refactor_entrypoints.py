from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(
    argv: list[str],
    *,
    cwd: Path | str = ROOT,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=check)


def _cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "src.interfaces.cli.main", *args], check=check)


def test_shell_entrypoints_work_outside_repo_cwd(tmp_path: Path) -> None:
    proc = _run([str((ROOT / "om").resolve()), "--help"], cwd=tmp_path, check=True)
    assert "usage:" in proc.stdout

    agent_proc = _run([str((ROOT / "om-agent").resolve()), "spec"], cwd=tmp_path, check=True)
    payload = json.loads(agent_proc.stdout)
    assert payload["name"] == "options-monitor-local-tools"


def test_unified_tick_help_works() -> None:
    proc = _cli("run", "tick", "--help")
    assert "run tick" in proc.stdout
    assert "--config" in proc.stdout


def test_unified_cli_validate_command_works_with_example_config(example_config_path: Path) -> None:
    proc = _cli("config", "validate", "--config-path", str(example_config_path))
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True


def test_agent_interface_spec_outputs_manifest() -> None:
    proc = _run([sys.executable, "-m", "src.interfaces.agent.cli", "spec"], check=True)
    payload = json.loads(proc.stdout)
    assert payload["name"] == "options-monitor-local-tools"
    assert any(str(item.get("name")) == "healthcheck" for item in payload.get("tools", []))


def test_unified_cli_scan_pipeline_command_exposes_canonical_flags() -> None:
    proc = _cli("scan-pipeline", "--help")
    assert "--report-dir" in proc.stdout
    assert "--shared-context-dir" in proc.stdout
    assert "--shared-scan-dir" not in proc.stdout
    assert "--reuse-shared-scan" not in proc.stdout


def test_unified_cli_option_positions_sync_feishu_command_is_removed() -> None:
    proc = _cli("option-positions", "sync-feishu", "--help", check=False)
    assert proc.returncode != 0
    assert "invalid choice" in proc.stderr


def test_unified_cli_option_positions_management_command_exists_without_legacy_market_alias() -> None:
    proc = _cli("option-positions", "list", "--help")
    assert "--broker" in proc.stdout
    assert "--market" not in proc.stdout


def test_unified_cli_option_performance_report_is_the_public_performance_entrypoint() -> None:
    proc = _cli("option-performance", "report", "--help")
    assert "--broker" in proc.stdout
    assert "--market" not in proc.stdout


def test_unified_cli_symbols_command_exists_without_legacy_script_path() -> None:
    proc = _cli("symbols", "list", "--help")
    assert "--format" in proc.stdout
    assert "scripts/watchlist.py" not in proc.stdout


def test_unified_cli_watchlist_command_is_removed() -> None:
    proc = _cli("watchlist", "list", "--help", check=False)
    assert proc.returncode != 0
