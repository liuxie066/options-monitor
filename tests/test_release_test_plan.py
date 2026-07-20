from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def test_release_test_plan_maps_event_and_service_changes() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=[
            "src/application/events/source_futu.py",
            "src/application/service_upgrade.py",
        ],
        mode="standard",
        version="1.2.183",
    )

    assert plan["ok"] is True
    assert plan["risk"] == "standard"
    assert plan["requires_full_pytest"] is False
    assert plan["commands"][0] == "./.venv/bin/python scripts/release_check.py --tag v1.2.183"
    assert "git diff --check" in plan["commands"]
    assert "./.venv/bin/python -m pytest tests/test_event_prefetch.py tests/test_event_source_futu.py tests/test_event_risk_warn.py" in plan["commands"]
    assert (
        "./.venv/bin/python -m pytest tests/test_service_deploy.py tests/test_release_version_recommendation.py "
        "tests/test_version_check.py tests/test_install_script.py tests/test_release_test_plan.py"
    ) in plan["commands"]
    assert "./.venv/bin/python scripts/generate_dependency_graph.py --check" in plan["commands"]
    assert {rule["name"] for rule in plan["matched_rules"]} >= {"event_source", "service_release"}


def test_release_test_plan_requires_full_pytest_for_ledger_changes() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["domain/domain/ledger/projection.py"],
        mode="fast",
        version="v1.2.183",
    )

    assert plan["risk"] == "full"
    assert plan["requires_full_pytest"] is True
    assert plan["commands"][0] == "./.venv/bin/python scripts/release_check.py --tag v1.2.183"
    assert plan["commands"][-1] == "./.venv/bin/python -m pytest"


def test_release_test_plan_maps_assistant_changes_to_minimal_runtime_gate() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=[
            "src/application/assistant/runtime.py",
            "src/application/agent_tools/analysis.py",
        ],
        mode="standard",
        version="1.2.184",
    )

    assert plan["risk"] == "standard"
    assert plan["requires_full_pytest"] is False
    assert {rule["name"] for rule in plan["matched_rules"]} >= {"assistant_runtime", "dependency_graph"}
    assert (
        "./.venv/bin/python -m pytest tests/test_assistant_runtime.py tests/test_inbound_control.py "
        "tests/test_assistant_permission_request.py tests/test_cli_operator_commands.py "
        "tests/test_assistant_diagnostics.py tests/test_architecture_guards.py"
    ) in plan["commands"]
    assert (
        "./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py "
        "tests/test_candidate_filter_trace.py tests/test_analysis_tools.py"
    ) in plan["commands"]
    assert all("test_assistant_agent_eval.py" not in command for command in plan["commands"])
    assert all("test_assistant_evidence_session.py" not in command for command in plan["commands"])
    assert all("test_assistant_context_projection.py" not in command for command in plan["commands"])
    assert all("test_assistant_context_validation.py" not in command for command in plan["commands"])


def test_release_test_plan_maps_config_validator_changes_to_config_gate() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["src/application/config_validator.py"],
        mode="standard",
        version="1.2.184",
    )

    assert {rule["name"] for rule in plan["matched_rules"]} >= {"config", "dependency_graph"}
    assert (
        "./.venv/bin/python -m pytest tests/test_config_yaml.py tests/test_layered_config.py "
        "tests/test_validate_config_notifications.py"
    ) in plan["commands"]


def test_release_test_plan_full_mode_always_adds_full_pytest() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(changed_files=["docs/RELEASE_PROCESS.md"], mode="full")

    assert plan["mode"] == "full"
    assert plan["risk"] == "full"
    assert plan["requires_full_pytest"] is True
    assert plan["commands"][-1] == "./.venv/bin/python -m pytest"


def test_changed_files_from_git_unions_base_staged_and_worktree(tmp_path: Path) -> None:
    from src.application.release_test_plan import changed_files_from_git

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        if command == ["git", "diff", "--name-only", "origin/main...HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/application/events/source_futu.py\n", stderr="")
        if command == ["git", "diff", "--name-only", "--cached"]:
            return subprocess.CompletedProcess(command, 0, stdout="docs/RELEASE_PROCESS.md\n", stderr="")
        if command == ["git", "diff", "--name-only"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="src/application/events/source_futu.py\nsrc/interfaces/cli/main.py\n",
                stderr="",
            )
        if command == ["git", "ls-files", "--others", "--exclude-standard"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/application/release_test_plan.py\n", stderr="")
        raise AssertionError(command)

    assert changed_files_from_git(base_ref="origin/main", run_cmd=_run_cmd, cwd=tmp_path) == [
        "docs/RELEASE_PROCESS.md",
        "src/application/events/source_futu.py",
        "src/application/release_test_plan.py",
        "src/interfaces/cli/main.py",
    ]


def test_release_test_plan_rejects_unknown_mode() -> None:
    from src.application.release_test_plan import build_release_test_plan

    with pytest.raises(ValueError, match="unsupported release test mode"):
        build_release_test_plan(changed_files=[], mode="overnight")


def test_python_ci_workflows_use_supported_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    workflows = (
        root / ".github" / "workflows" / "agent-plugin.yml",
        root / ".github" / "workflows" / "guardrails.yml",
        root / ".github" / "workflows" / "_release-reusable.yml",
    )

    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert 'python-version: "3.12"' in text or "python-version: '3.12'" in text
        assert 'python-version: "3.11"' not in text
        assert "python-version: '3.11'" not in text


def _run_release_preflight_with_fake_python(tmp_path: Path, *args: str) -> list[str]:
    root = Path(__file__).resolve().parents[1]
    log_path = tmp_path / "python-commands.log"
    fake_python = tmp_path / "python3.12"
    fake_python.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-c" ]]; then
  printf '3.12.0\\n'
  exit 0
fi
if [[ "${1:-}" == "--version" ]]; then
  printf 'Python 3.12.0\\n'
  exit 0
fi
printf '%s\\n' "$*" >> "${OM_TEST_PYTHON_LOG}"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = dict(os.environ)
    env.update({"OM_PYTHON": str(fake_python), "OM_TEST_PYTHON_LOG": str(log_path)})

    proc = subprocess.run(
        ["bash", "scripts/release_preflight.sh", "--allow-dirty", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    return log_path.read_text(encoding="utf-8").splitlines()


def test_release_preflight_full_mode_runs_pytest_once(tmp_path: Path) -> None:
    commands = _run_release_preflight_with_fake_python(tmp_path, "--full")

    pytest_commands = [command for command in commands if command.startswith("-m pytest")]
    assert pytest_commands == ["-m pytest"]


def test_release_preflight_non_full_mode_keeps_focused_tests(tmp_path: Path) -> None:
    commands = _run_release_preflight_with_fake_python(tmp_path)

    pytest_commands = [command for command in commands if command.startswith("-m pytest")]
    assert pytest_commands == [
        "-m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py"
    ]


def test_release_version_recommendation_maps_to_release_focused_tests() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["src/application/release_version_recommendation.py"],
        mode="standard",
    )

    rules = {rule["name"] for rule in plan["matched_rules"]}
    assert {"service_release", "dependency_graph"} <= rules
    assert any("tests/test_release_version_recommendation.py" in command for command in plan["commands"])
