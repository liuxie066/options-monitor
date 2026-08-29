from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def test_release_test_plan_maps_service_changes_without_retired_event_suite() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=[
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
    assert not any("test_event_prefetch.py" in command for command in plan["commands"])
    assert (
        "./.venv/bin/python -m pytest tests/*/test_service_deploy_*.py "
        "tests/test_release_check.py "
        "tests/test_release_delta_coverage.py "
        "tests/test_release_version_recommendation.py tests/test_version_check.py "
        "tests/test_install_script.py tests/test_release_test_plan.py"
    ) in plan["commands"]
    assert "./.venv/bin/python scripts/generate_dependency_graph.py --check" in plan["commands"]
    assert {rule["name"] for rule in plan["matched_rules"]} >= {"service_release"}


def test_release_test_plan_maps_service_deploy_support_to_both_runtime_gates() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["tests/service_deploy_test_support.py"],
        mode="standard",
    )

    assert {"service_release", "pi_runtime"} <= {rule["name"] for rule in plan["matched_rules"]}
    assert any("tests/*/test_service_deploy_*.py" in command for command in plan["commands"])


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
            "docs/OM_COPILOT_V2_DESIGN.md",
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


@pytest.mark.parametrize(
    "changed_file",
    [
        ".github/workflows/_release-reusable.yml",
        ".github/workflows/guardrails.yml",
        "agent-runtime/package.json",
        "agent-runtime/package-lock.json",
        "agent-runtime/main.ts",
        "scripts/copilot_p1_eval.py",
        "scripts/install.sh",
        "scripts/release_preflight.sh",
        "scripts/pi_runtime_smoke.sh",
        "src/application/copilot/host.py",
        "src/application/release_test_plan.py",
        "src/application/service_upgrade.py",
        "src/application/setup/check.py",
        "src/infrastructure/pi_agent_process.py",
        "docs/PI_AGENT_CORE_INTEGRATION.md",
        "tests/copilot_pi_test_support.py",
        "tests/test_architecture_guards.py",
        "tests/test_pi_agent_process.py",
        "tests/test_copilot_p1_eval.py",
        "tests/test_copilot_phase1.py",
        "tests/test_copilot_conversation_memory.py",
        "tests/test_inbound_control.py",
        "tests/test_setup_check.py",
        "tests/test_cli_operator_commands.py",
        "tests/test_install_script.py",
        "tests/service_deploy_test_support.py",
        "tests/unit/test_service_deploy_unit.py",
        "tests/integration/test_service_deploy_integration.py",
        "tests/e2e/test_service_deploy_e2e.py",
        "tests/test_release_check.py",
        "tests/test_release_test_plan.py",
        "tests/copilot_eval/test_answer_quality.py",
    ],
)
def test_release_test_plan_maps_every_pi_runtime_surface(changed_file: str) -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(changed_files=[changed_file], mode="standard")

    assert "pi_runtime" in {rule["name"] for rule in plan["matched_rules"]}
    assert "npm ci --omit=dev --ignore-scripts --prefix agent-runtime" in plan["commands"]
    assert any("tests/test_pi_agent_process.py" in command for command in plan["commands"])
    assert any("tests/test_copilot_p1_eval.py" in command for command in plan["commands"])
    assert any("tests/copilot_eval/test_answer_quality.py" in command for command in plan["commands"])


def test_release_preflight_maps_to_service_and_pi_runtime_gates() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(changed_files=["scripts/release_preflight.sh"], mode="standard")

    matched_rules = {rule["name"] for rule in plan["matched_rules"]}
    assert {"service_release", "pi_runtime"} <= matched_rules
    assert "npm ci --omit=dev --ignore-scripts --prefix agent-runtime" in plan["commands"]
    assert any("tests/test_pi_agent_process.py" in command for command in plan["commands"])
    assert any("tests/test_copilot_p1_eval.py" in command for command in plan["commands"])
    assert any("tests/copilot_eval/test_answer_quality.py" in command for command in plan["commands"])
    assert any("tests/test_release_test_plan.py" in command for command in plan["commands"])


def test_release_test_plan_requires_current_taxonomy_when_version_changes() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["VERSION", "CHANGELOG.md"],
        mode="standard",
        version="1.5.0",
    )

    assert plan["commands"][0] == (
        "./.venv/bin/python scripts/release_check.py --tag v1.5.0 --require-current-taxonomy --require-delta-coverage"
    )


def test_release_test_plan_requires_delta_coverage_for_manifest_change() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["release/coverage/v1.5.0.json"],
        mode="standard",
        version="1.5.0",
    )

    assert plan["commands"][0] == ("./.venv/bin/python scripts/release_check.py --tag v1.5.0 --require-delta-coverage")
    assert {rule["name"] for rule in plan["matched_rules"]} == {"service_release"}


def test_release_test_plan_maps_current_copilot_design_doc() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["docs/OM_COPILOT_V2_DESIGN.md"],
        mode="standard",
    )

    assert {rule["name"] for rule in plan["matched_rules"]} == {"assistant_runtime"}


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


def test_release_workflow_runs_required_control_plane_suites() -> None:
    root = Path(__file__).resolve().parents[1]
    required_suites = (
        "tests/test_config_yaml.py",
        "tests/test_config_template_inheritance.py",
        "tests/test_config_authoring_transaction.py",
        "tests/test_runtime_config_identity.py",
        "tests/*/test_service_deploy_*.py",
        "tests/test_inbound_control.py",
        "tests/test_setup_check.py",
        "tests/test_cli_operator_commands.py",
    )
    workflows = (root / ".github" / "workflows" / "_release-reusable.yml",)

    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        for suite in required_suites:
            assert suite in text, f"{workflow.name} is missing required suite {suite}"


def test_required_pr_guardrail_discovers_full_suite_after_pi_smoke() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "guardrails.yml").read_text(encoding="utf-8")

    assert text.count("uses: actions/setup-node@v4") == 1
    assert text.count("node-version: '22.19.0'") == 1
    assert "npm ci --omit=dev --ignore-scripts --prefix agent-runtime" in text
    assert (
        'bash scripts/pi_runtime_smoke.sh --root "${{ github.workspace }}" '
        '--python "${{ github.workspace }}/.venv/bin/python"'
    ) in text
    assert "npm view" not in text
    assert [line.strip() for line in text.splitlines() if line.strip() == "./.venv/bin/python -m pytest"] == [
        "./.venv/bin/python -m pytest"
    ]
    assert "tests/test_" not in text
    assert (
        text.index("npm ci --omit=dev --ignore-scripts --prefix agent-runtime")
        < text.index("scripts/pi_runtime_smoke.sh")
        < text.index("tests/run_smoke.py")
        < text.index("./.venv/bin/python -m pytest")
    )


def test_release_workflow_pins_and_gates_pi_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = root / ".github" / "workflows" / "_release-reusable.yml"
    required_suites = (
        "tests/test_pi_agent_process.py",
        "tests/test_copilot_p1_eval.py",
        "tests/test_copilot_phase1.py",
        "tests/test_copilot_conversation_memory.py",
        "tests/test_inbound_control.py",
        "tests/test_setup_check.py",
        "tests/test_cli_operator_commands.py",
        "tests/test_install_script.py",
        "tests/*/test_service_deploy_*.py",
        "tests/test_release_check.py",
        "tests/test_release_test_plan.py",
        "tests/copilot_eval/test_answer_quality.py",
    )

    text = workflow.read_text(encoding="utf-8")
    assert text.count("uses: actions/setup-node@v4") == 1
    assert text.count("node-version: '22.19.0'") == 1
    assert "npm ci --omit=dev --ignore-scripts --prefix agent-runtime" in text
    assert (
        'bash scripts/pi_runtime_smoke.sh --root "${{ github.workspace }}" '
        '--python "${{ github.workspace }}/.venv/bin/python"'
    ) in text
    assert "npm view" not in text
    assert (
        text.index("npm ci --omit=dev --ignore-scripts --prefix agent-runtime")
        < text.index("scripts/pi_runtime_smoke.sh")
        < text.index("tests/test_pi_agent_process.py")
    )
    for suite in required_suites:
        assert suite in text, f"{workflow.name} is missing Pi suite {suite}"


def test_release_workflow_verifies_extracted_archive_before_publish() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/_release-reusable.yml").read_text(encoding="utf-8")

    build_at = text.index("- name: Build source archive")
    verify_at = text.index("- name: Verify source archive Pi runtime")
    publish_at = text.index("- name: Publish release")
    assert build_at < verify_at < publish_at

    verify = text[verify_at:publish_at]
    assert 'CHECKOUT_PYTHON="${{ github.workspace }}/.venv/bin/python"' in verify
    assert 'ARCHIVE_ROOT="$(mktemp -d "${RUNNER_TEMP}/options-monitor-archive.XXXXXX")"' in verify
    assert 'tar -xzf "options-monitor-${{ inputs.tag }}.tar.gz" -C "${ARCHIVE_ROOT}"' in verify
    assert 'cd "${ARCHIVE_ROOT}"' in verify
    assert 'npm ci --omit=dev --ignore-scripts --prefix "${ARCHIVE_ROOT}/agent-runtime"' in verify
    assert (
        'bash "${ARCHIVE_ROOT}/scripts/pi_runtime_smoke.sh" --root "${ARCHIVE_ROOT}" --python "${CHECKOUT_PYTHON}"'
    ) in verify
    assert "--prefix agent-runtime" not in verify
    assert '--python ".venv/bin/python"' not in verify


def test_version_release_reuses_successful_guardrails_without_duplicate_regressions() -> None:
    root = Path(__file__).resolve().parents[1]
    guardrails = (root / ".github/workflows/guardrails.yml").read_text(encoding="utf-8")
    reusable = (root / ".github/workflows/_release-reusable.yml").read_text(encoding="utf-8")
    manual = (root / ".github/workflows/release-from-version.yml").read_text(encoding="utf-8")

    assert "contains(github.event.head_commit.modified, 'VERSION')" not in guardrails
    assert "BEFORE_SHA: ${{ github.event.before }}" in guardrails
    assert 'git fetch --no-tags --depth=1 origin "${BEFORE_SHA}"' in guardrails
    assert 'git diff --quiet "${BEFORE_SHA}" "${GITHUB_SHA}" -- VERSION' in guardrails
    assert 'if [[ "${DIFF_STATUS}" -ne 1 ]]' in guardrails
    assert "uses: ./.github/workflows/_release-reusable.yml" in guardrails
    assert "run_regression_gates: false" in guardrails
    assert reusable.count("if: ${{ inputs.run_regression_gates }}") == 4
    assert "default: true" in reusable
    assert "\n  push:" not in manual
    assert "run_regression_gates: true" in manual


def _run_release_preflight_with_fake_python(
    tmp_path: Path,
    *args: str,
    node_version: str = "v22.19.0",
    loopback_bind_denied: bool = False,
    cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    root = Path(__file__).resolve().parents[1]
    log_path = tmp_path / "python-commands.log"
    fake_python = tmp_path / "python3.12"
    fake_python.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-c" && "${2:-}" == *"OM_RELEASE_PREFLIGHT_LOOPBACK_PROBE"* ]]; then
  if [[ "${OM_TEST_LOOPBACK_BIND_DENIED:-0}" == "1" ]]; then
    printf 'PermissionError: [Errno 1] Operation not permitted\\n' >&2
    exit 77
  fi
  exit 0
fi
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
    fake_node = tmp_path / "node"
    fake_node.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then
  printf '%s\\n' "${OM_TEST_NODE_VERSION}"
fi
""",
        encoding="utf-8",
    )
    fake_node.chmod(0o755)
    fake_npm = tmp_path / "npm"
    fake_npm.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'npm %s\\n' "$*" >> "${OM_TEST_PYTHON_LOG}"
""",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "OM_PYTHON": str(fake_python),
            "OM_TEST_PYTHON_LOG": str(log_path),
            "OM_TEST_NODE_VERSION": node_version,
            "OM_TEST_LOOPBACK_BIND_DENIED": "1" if loopback_bind_denied else "0",
            "PATH": f"{tmp_path}{os.pathsep}{env['PATH']}",
        }
    )

    proc = subprocess.run(
        ["bash", str(root / "scripts" / "release_preflight.sh"), "--allow-dirty", *args],
        cwd=cwd or root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    commands = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return proc, commands


def test_release_preflight_full_mode_runs_pytest_once(tmp_path: Path) -> None:
    proc, commands = _run_release_preflight_with_fake_python(tmp_path, "--full")

    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "[PREFLIGHT_OK] loopback bind available (127.0.0.1)" in proc.stdout
    pytest_commands = [command for command in commands if command.startswith("-m pytest")]
    assert pytest_commands == ["-m pytest"]
    assert commands.count("npm ci --omit=dev --ignore-scripts --prefix agent-runtime") == 1
    assert any("copilot eval --fixture current_option_exposure_model_ready" in command for command in commands)


def test_release_preflight_exports_selected_python_to_nested_entrypoints() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "release_preflight.sh").read_text(encoding="utf-8")

    assert 'export OM_PYTHON="${PYTHON_BIN}"' in text


def test_release_preflight_non_full_mode_keeps_focused_tests(tmp_path: Path) -> None:
    proc, commands = _run_release_preflight_with_fake_python(tmp_path)

    assert proc.returncode == 0, proc.stderr or proc.stdout
    pytest_commands = [command for command in commands if command.startswith("-m pytest")]
    assert pytest_commands == [
        "-m pytest tests/test_pi_agent_process.py",
        (
            "-m pytest tests/test_copilot_phase1.py tests/test_copilot_conversation_memory.py "
            "tests/test_copilot_p1_eval.py tests/test_inbound_control.py "
            "tests/test_setup_check.py "
            "tests/test_cli_operator_commands.py tests/test_install_script.py "
            "tests/e2e/test_service_deploy_e2e.py "
            "tests/integration/test_service_deploy_integration.py "
            "tests/unit/test_service_deploy_unit.py tests/test_release_check.py "
            "tests/test_release_test_plan.py tests/copilot_eval/test_answer_quality.py"
        ),
        "-m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py",
        (
            "-m pytest tests/test_research.py tests/test_research_archive.py "
            "tests/test_shadow_replay.py tests/test_shadow_replay_candidate_impact.py "
            "tests/test_strategy_lab_update.py tests/test_strategy_lab_top1_architecture.py"
        ),
        (
            "-m pytest tests/test_config_yaml.py tests/test_config_template_inheritance.py "
            "tests/test_config_authoring_transaction.py tests/test_runtime_config_identity.py"
        ),
    ]
    assert commands.count("npm ci --omit=dev --ignore-scripts --prefix agent-runtime") == 1
    assert any("copilot eval --fixture current_option_exposure_model_ready" in command for command in commands)


def test_release_preflight_focused_mode_is_independent_of_caller_cwd(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    proc, commands = _run_release_preflight_with_fake_python(tmp_path, cwd=outside)

    assert proc.returncode == 0, proc.stderr or proc.stdout
    pytest_commands = [command for command in commands if command.startswith("-m pytest")]
    service_command = next(command for command in pytest_commands if "test_service_deploy" in command)
    assert "tests/unit/test_service_deploy_unit.py" in service_command
    assert "tests/integration/test_service_deploy_integration.py" in service_command
    assert "tests/e2e/test_service_deploy_e2e.py" in service_command
    assert "tests/*/test_service_deploy_*.py" not in service_command


def test_release_preflight_rejects_old_node_before_npm(tmp_path: Path) -> None:
    proc, commands = _run_release_preflight_with_fake_python(tmp_path, node_version="v22.18.9")

    assert proc.returncode == 1
    assert "Node >=22.19.0 is required; observed=v22.18.9" in proc.stderr
    assert not any(command.startswith("npm ") for command in commands)


def test_release_preflight_rejects_sandbox_loopback_denial_before_npm(tmp_path: Path) -> None:
    proc, commands = _run_release_preflight_with_fake_python(
        tmp_path,
        "--full",
        loopback_bind_denied=True,
    )

    assert proc.returncode == 1
    assert (
        "loopback bind denied: 127.0.0.1 socket.bind() returned PermissionError: [Errno 1] Operation not permitted"
    ) in proc.stderr
    assert "Rerun this unchanged preflight outside the sandbox" in proc.stderr
    assert "do not skip or xfail the tests" in proc.stderr
    assert "No release or remote upgrade has started" in proc.stderr
    assert commands == []


def test_release_preflight_skips_loopback_probe_when_no_listener_tests_run(tmp_path: Path) -> None:
    proc, commands = _run_release_preflight_with_fake_python(
        tmp_path,
        "--skip-focused",
        loopback_bind_denied=True,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "loopback bind" not in proc.stdout
    assert "loopback bind" not in proc.stderr
    assert not any(command.startswith("-m pytest") for command in commands)


def test_release_version_recommendation_maps_to_release_focused_tests() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["src/application/release_version_recommendation.py"],
        mode="standard",
    )

    rules = {rule["name"] for rule in plan["matched_rules"]}
    assert {"service_release", "dependency_graph"} <= rules
    assert any("tests/test_release_version_recommendation.py" in command for command in plan["commands"])
