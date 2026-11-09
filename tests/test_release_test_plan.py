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

    assert {"service_release", "bot_runtime"} <= {rule["name"] for rule in plan["matched_rules"]}
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


def test_release_test_plan_maps_bot_changes_to_minimal_runtime_gate() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=[
            "docs/BOT_DESIGN.md",
            "src/application/bot/control/runtime.py",
            "src/application/agent_tool_registry.py",
        ],
        mode="standard",
        version="1.2.184",
    )

    assert plan["risk"] == "standard"
    assert plan["requires_full_pytest"] is False
    assert {rule["name"] for rule in plan["matched_rules"]} >= {"bot_runtime", "dependency_graph"}
    assert (
        "./.venv/bin/python -m pytest tests/test_bot_runtime.py tests/test_inbound_control.py "
        "tests/test_bot_permission_request.py tests/test_cli_operator_commands.py "
        "tests/test_bot_diagnostics.py tests/test_architecture_guards.py"
    ) in plan["commands"]
    assert (
        "./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py "
        "tests/test_candidate_filter_trace.py"
    ) in plan["commands"]
    assert all("test_bot_agent_eval.py" not in command for command in plan["commands"])
    assert all("test_bot_evidence_session.py" not in command for command in plan["commands"])
    assert all("test_bot_context_projection.py" not in command for command in plan["commands"])
    assert all("test_bot_context_validation.py" not in command for command in plan["commands"])


@pytest.mark.parametrize(
    "changed_file",
    [
        ".github/workflows/_release-reusable.yml",
        ".github/workflows/guardrails.yml",
        "scripts/bot_p1_eval.py",
        "scripts/install.sh",
        "scripts/release_preflight.sh",
        "src/application/bot/runtime.py",
        "src/application/bot/host.py",
        "src/application/release_test_plan.py",
        "src/application/service_upgrade.py",
        "src/application/setup/check.py",
        "tests/test_architecture_guards.py",
        "tests/test_bot_python_runtime.py",
        "tests/test_pi_retirement.py",
        "tests/test_bot_p1_eval.py",
        "tests/test_bot_phase1.py",
        "tests/test_bot_conversation_memory.py",
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
        "tests/bot_eval/test_answer_quality.py",
    ],
)
def test_release_test_plan_maps_every_python_runtime_surface(changed_file: str) -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(changed_files=[changed_file], mode="standard")

    assert "bot_runtime" in {rule["name"] for rule in plan["matched_rules"]}
    assert not any(command.startswith("npm ") for command in plan["commands"])
    assert any("tests/test_bot_python_runtime.py" in command for command in plan["commands"])
    assert any("tests/test_bot_p1_eval.py" in command for command in plan["commands"])
    assert any("tests/bot_eval/test_answer_quality.py" in command for command in plan["commands"])



@pytest.mark.parametrize(
    "changed_file",
    ["src/application/service_upgrade.py", "src/application/service_cleanup.py", "tests/test_pi_retirement.py"],
)
def test_release_test_plan_selects_python_target_retirement_regressions(changed_file: str) -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(changed_files=[changed_file], mode="standard")

    assert "service_release" in {rule["name"] for rule in plan["matched_rules"]}
    assert plan["commands"].count("./.venv/bin/python -m pytest tests/test_pi_retirement.py") == 1
    assert not plan["requires_full_pytest"]
    # Every selected suite must exist after retirement; stale test names must not
    # turn a generated release plan into a collection error.
    root = Path(__file__).resolve().parents[1]
    for command in plan["commands"]:
        if " -m pytest " in command:
            for target in command.split(" -m pytest ", 1)[1].split():
                assert list(root.glob(target)), f"release plan selected missing test target: {target}"


def test_release_preflight_maps_to_service_and_python_runtime_gates() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(changed_files=["scripts/release_preflight.sh"], mode="standard")

    matched_rules = {rule["name"] for rule in plan["matched_rules"]}
    assert {"service_release", "bot_runtime"} <= matched_rules
    assert not any(command.startswith("npm ") for command in plan["commands"])
    assert any("tests/test_bot_python_runtime.py" in command for command in plan["commands"])
    assert any("tests/test_bot_p1_eval.py" in command for command in plan["commands"])
    assert any("tests/bot_eval/test_answer_quality.py" in command for command in plan["commands"])
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


def test_release_test_plan_maps_current_bot_design_doc() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=["docs/BOT_DESIGN.md"],
        mode="standard",
    )

    assert {rule["name"] for rule in plan["matched_rules"]} == {"bot_runtime"}


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
    text = (root / ".github/workflows/guardrails.yml").read_text()
    assert "-m pytest -vv -n 4 --dist loadfile" in text
    for suite in required_suites:
        assert list(root.glob(suite)), f"missing full-discovery suite {suite}"


def test_required_pr_and_release_guardrail_verifies_complete_distributed_suite() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "guardrails.yml").read_text(encoding="utf-8")
    requirements = (root / "requirements" / "dev.txt").read_text(encoding="utf-8").splitlines()
    constraints = (root / "constraints" / "dev.txt").read_text(encoding="utf-8").splitlines()
    full_command = (
        "./.venv/bin/python -m pytest -vv -n 4 --dist loadfile "
        "--max-worker-restart=0 --durations=25"
    )
    pytest_commands = [
        line.strip().removesuffix("\\").rstrip()
        for line in text.splitlines() if "./.venv/bin/python -m pytest" in line
    ]

    assert not (root / ".github" / "workflows" / "agent-plugin.yml").exists()
    assert "actions/setup-node" not in text
    assert "npm ci" not in text
    assert "./om-agent spec > /tmp/om-agent-spec.json" in text
    assert "github.event_name == 'pull_request' || needs.plan.outputs.release_tag != ''" in text
    assert (
        text.index("--check-sensitive-artifacts")
        < text.index("- name: Public surface check")
        < text.index("tests/run_smoke.py")
    )
    surface_step = text.split("- name: Public surface check", 1)[1].split("- name: Standalone smoke", 1)[0]
    assert "if: ${{ github.event_name == 'pull_request' }}" in surface_step
    assert "git merge-base" in surface_step
    assert "github.event.pull_request.base.ref" in surface_step
    assert "--check-public-surface" in surface_step
    assert "--public-surface-base" in surface_step
    # A merge base is only as trustworthy as the history behind it, and a
    # single-commit checkout cannot resolve one at all.
    checkout_step = text.split("- name: Checkout", 1)[1].split("- name: Set up Python", 1)[0]
    assert "fetch-depth: 0" in checkout_step
    assert pytest_commands == [full_command]
    assert "-o tmp_path_retention_policy=failed" in text
    assert not any(selector in full_command for selector in (" -k ", "--ignore", "--deselect", "tests/test_"))
    assert "tests/test_" not in text
    assert "pytest-xdist" in requirements
    assert "pytest-xdist==3.8.0" in constraints
    assert text.index("tests/run_smoke.py") < text.index("./om-agent spec")
    assert "shard: [0, 1, 2, 3, 4, 5, 6, 7]" in text
    assert "fail-fast: false" in text
    assert '-p pytest_shard --ci-shard "$CI_SHARD" --ci-shard-count 8' in text
    assert '--ci-revision "$GITHUB_SHA"' in text
    gate = text.split("\n  guardrails:", 1)[1].split("\n  release:", 1)[0]
    assert "needs: [plan, checks, regression]" in gate
    assert "if: ${{ always() }}" in gate
    assert 'test "$PLAN_RESULT" = success' in gate
    assert 'test "$PLAN_RESULT" = skipped' in gate
    assert 'if [ "$EVENT_NAME" = pull_request ]; then' in gate
    plan_job = text.split("\n  plan:", 1)[1].split("\n  checks:", 1)[0]
    assert "if: github.event_name != 'pull_request'" in plan_job
    assert 'test "$CHECKS_RESULT" = success' in gate
    assert 'test "$REGRESSION_RESULT" = success' in gate
    assert 'test "$REGRESSION_RESULT" = skipped' in gate
    assert 'python .github/ci/regression_manifest.py' in gate
    assert '--count 8 --revision "$GITHUB_SHA"' in gate


def test_release_workflow_gates_python_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = root / ".github" / "workflows" / "_release-reusable.yml"
    required_suites = (
        "tests/test_bot_python_runtime.py",
        "tests/test_bot_p1_eval.py",
        "tests/test_bot_phase1.py",
        "tests/test_bot_conversation_memory.py",
        "tests/test_inbound_control.py",
        "tests/test_setup_check.py",
        "tests/test_cli_operator_commands.py",
        "tests/test_install_script.py",
        "tests/*/test_service_deploy_*.py",
        "tests/test_release_check.py",
        "tests/test_release_test_plan.py",
        "tests/bot_eval/test_answer_quality.py",
    )

    text = workflow.read_text(encoding="utf-8")
    assert "actions/setup-node" not in text
    assert "npm ci" not in text
    guardrails = (root / ".github/workflows/guardrails.yml").read_text()
    assert "-m pytest -vv -n 4 --dist loadfile" in guardrails
    for suite in required_suites:
        assert list(root.glob(suite)), f"missing full-discovery suite {suite}"


def test_release_workflow_verifies_extracted_archive_before_publish() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/_release-reusable.yml").read_text(encoding="utf-8")

    build_at = text.index("- name: Build source archive")
    verify_at = text.index("- name: Verify source archive Python Bot")
    publish_at = text.index("- name: Publish release")
    assert build_at < verify_at < publish_at

    verify = text[verify_at:publish_at]
    assert 'CHECKOUT_PYTHON="${{ github.workspace }}/.venv/bin/python"' in verify
    assert 'ARCHIVE_ROOT="$(mktemp -d "${RUNNER_TEMP}/options-monitor-archive.XXXXXX")"' in verify
    assert 'tar -xzf "options-monitor-${{ inputs.tag }}.tar.gz" -C "${ARCHIVE_ROOT}"' in verify
    assert 'cd "${ARCHIVE_ROOT}"' in verify
    assert '"${CHECKOUT_PYTHON}" -c' in verify
    assert "import src.application.bot.runtime" in verify
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
    assert "run_regression_gates" not in guardrails + reusable
    assert "./.github/workflows/guardrails.yml" in manual
    assert "-m pytest" not in reusable
    assert "\n  push:" not in manual
    assert "run_regression_gates" not in manual


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
    assert pytest_commands == [
        "-m pytest -n 4 --dist loadfile --max-worker-restart=0 -o tmp_path_retention_policy=failed"
    ]
    assert not any(command.startswith("npm ") for command in commands)


def test_release_preflight_exports_selected_python_to_nested_entrypoints() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "release_preflight.sh").read_text(encoding="utf-8")

    assert 'export OM_PYTHON="${PYTHON_BIN}"' in text


def test_release_preflight_non_full_mode_keeps_focused_tests(tmp_path: Path) -> None:
    proc, commands = _run_release_preflight_with_fake_python(tmp_path)

    assert proc.returncode == 0, proc.stderr or proc.stdout
    pytest_commands = [command for command in commands if command.startswith("-m pytest")]
    assert pytest_commands == [
        "-m pytest tests/test_bot_python_runtime.py",
        (
            "-m pytest tests/test_bot_phase1.py tests/test_bot_conversation_memory.py "
            "tests/test_bot_p1_eval.py tests/test_inbound_control.py "
            "tests/test_setup_check.py "
            "tests/test_cli_operator_commands.py tests/test_install_script.py "
            "tests/e2e/test_service_deploy_e2e.py "
            "tests/integration/test_service_deploy_integration.py "
            "tests/unit/test_service_deploy_unit.py tests/test_release_check.py "
            "tests/test_release_test_plan.py tests/bot_eval/test_answer_quality.py"
        ),
        "-m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py",
        "-m pytest tests/test_research.py tests/test_research_archive.py",
        (
            "-m pytest tests/test_config_yaml.py tests/test_config_template_inheritance.py "
            "tests/test_config_authoring_transaction.py tests/test_runtime_config_identity.py"
        ),
    ]
    assert not any(command.startswith("npm ") for command in commands)


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


def test_release_preflight_does_not_require_node(tmp_path: Path) -> None:
    proc, commands = _run_release_preflight_with_fake_python(tmp_path, node_version="v22.18.9")

    assert proc.returncode == 0, proc.stderr
    assert not any(command.startswith("npm ") for command in commands)


def test_release_preflight_rejects_sandbox_loopback_denial_before_tests(tmp_path: Path) -> None:
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


def _git_discovery_failed_runner(failed_index: int, calls: list[list[str]]):
    def run(command, **kwargs):
        calls.append(list(command))
        assert kwargs == {
            "cwd": str(Path.cwd()), "capture_output": True, "text": True, "check": False,
        }
        if len(calls) - 1 == failed_index:
            return subprocess.CompletedProcess(
                command, 128,
                stdout="src/application/unreliable_partial_result.py\n",
                stderr="synthetic Git discovery failure\n",
            )
        return subprocess.CompletedProcess(
            command, 0, stdout="domain/domain/ledger/projection.py\n", stderr="",
        )

    return run


@pytest.mark.parametrize("failed_index", range(4), ids=["committed", "staged", "unstaged", "untracked"])
def test_git_discovery_rejects_each_failed_command(failed_index: int) -> None:
    from src.application.release_test_plan import changed_files_from_git

    calls = []
    with pytest.raises(subprocess.CalledProcessError) as caught:
        changed_files_from_git(
            base_ref="HEAD", cwd=Path.cwd(),
            run_cmd=_git_discovery_failed_runner(failed_index, calls),
        )
    assert len(calls) == failed_index + 1
    assert caught.value.cmd == calls[-1]
    assert caught.value.returncode == 128
    assert caught.value.stdout == "src/application/unreliable_partial_result.py\n"
    assert caught.value.stderr == "synthetic Git discovery failure\n"


@pytest.mark.parametrize("failed_index", range(4), ids=["committed", "staged", "unstaged", "untracked"])
def test_git_discovery_cli_emits_no_plan_after_partial_failure(monkeypatch, capsys, failed_index: int) -> None:
    from argparse import Namespace
    from scripts import release_test_plan as cli
    from src.application.release_test_plan import changed_files_from_git

    calls = []
    builds = []
    real_build = cli.build_release_test_plan

    def discover(**kwargs):
        return changed_files_from_git(run_cmd=_git_discovery_failed_runner(failed_index, calls), **kwargs)

    def build(**kwargs):
        builds.append(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(cli, "parse_args", lambda: Namespace(
        base="HEAD", mode="standard", tag=None, changed_file=None,
    ))
    monkeypatch.setattr(cli, "changed_files_from_git", discover)
    monkeypatch.setattr(cli, "build_release_test_plan", build)
    assert cli.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "128" in captured.err
    assert "synthetic Git discovery failure" in captured.err
    assert len(calls) == failed_index + 1
    assert builds == []


def _git_discovery_git_env(tmp_path: Path) -> dict[str, str]:
    """Build an empty, valid Git history without touching the task's .git.

    Loose objects avoid init/add/commit commands, hooks, and real Git config.
    Git itself still performs every discovery operation used by the CLI.
    """
    import hashlib
    import zlib

    git_dir = tmp_path / "synthetic-git"
    work_tree = tmp_path / "synthetic-work-tree"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs" / "heads").mkdir(parents=True)
    work_tree.mkdir()

    def write_object(kind: str, body: bytes) -> str:
        raw = f"{kind} {len(body)}\0".encode("ascii") + body
        oid = hashlib.sha1(raw).hexdigest()
        object_path = git_dir / "objects" / oid[:2] / oid[2:]
        object_path.parent.mkdir(exist_ok=True)
        object_path.write_bytes(zlib.compress(raw))
        return oid

    tree = write_object("tree", b"")
    commit = write_object(
        "commit",
        (
            f"tree {tree}\n"
            "author Synthetic Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "committer Synthetic Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "\nSynthetic empty history for issue 708\n"
        ).encode("ascii"),
    )
    (git_dir / "HEAD").write_text("ref: refs/heads/control\n", encoding="ascii")
    (git_dir / "refs" / "heads" / "control").write_text(commit + "\n", encoding="ascii")
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n", encoding="ascii"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(work_tree),
            "GIT_INDEX_FILE": str(git_dir / "index"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OM_SECRET_BACKEND": "env",
        }
    )
    return env


def _git_discovery_run(
    env: dict[str, str], *command: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _git_discovery_cli(
    env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "release_test_plan.py"
    return _git_discovery_run(env, sys.executable, "-B", str(script), *args)


def _git_discovery_summary(proc: subprocess.CompletedProcess[str]) -> str:
    return (
        f"input={proc.args!r}; exit={proc.returncode}; "
        f"stdout={proc.stdout[:4000]!r}; stderr={proc.stderr[:4000]!r}"
    )


def test_git_discovery_control_valid_empty_head(tmp_path: Path) -> None:
    import json

    env = _git_discovery_git_env(tmp_path)
    for args in (
        ("diff", "--name-only", "HEAD...HEAD"),
        ("diff", "--name-only", "--cached"),
        ("diff", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        discovery = _git_discovery_run(env, "git", *args)
        assert discovery.returncode == 0 and not discovery.stdout.strip(), (
            "Valid zero-change fixture required: " + _git_discovery_summary(discovery)
        )

    proc = _git_discovery_cli(env, "--base", "HEAD")
    summary = _git_discovery_summary(proc)
    assert proc.returncode == 0, summary
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True, summary
    assert plan["changed_files"] == [], summary
    assert plan["matched_rules"] == [], summary
    assert plan["requires_full_pytest"] is False, summary


def test_git_discovery_control_explicit_ledger_mapping(tmp_path: Path) -> None:
    import json

    env = _git_discovery_git_env(tmp_path)
    changed_file = "domain/domain/ledger/projection.py"
    proc = _git_discovery_cli(env, "--changed-file", changed_file)
    summary = _git_discovery_summary(proc)
    assert proc.returncode == 0, summary
    plan = json.loads(proc.stdout)
    assert plan["ok"] is True, summary
    assert plan["changed_files"] == [changed_file], summary
    assert "ledger_positions_trades" in {rule["name"] for rule in plan["matched_rules"]}, summary
    assert plan["requires_full_pytest"] is True, summary
    assert "./.venv/bin/python -m pytest" in plan["commands"], summary


def test_git_discovery_regression_absent_base_is_failure(tmp_path: Path) -> None:
    import json

    env = _git_discovery_git_env(tmp_path)
    base = "refs/heads/om-issue-708-deliberately-absent"
    failed_discovery = _git_discovery_run(
        env, "git", "diff", "--name-only", f"{base}...HEAD"
    )
    assert failed_discovery.returncode == 128 and base in failed_discovery.stderr, (
        "Original missing-base trigger required: "
        + _git_discovery_summary(failed_discovery)
    )
    for args in (
        ("diff", "--name-only", "--cached"),
        ("diff", "--name-only"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        discovery = _git_discovery_run(env, "git", *args)
        assert discovery.returncode == 0 and not discovery.stdout.strip(), (
            "Other three discovery lanes must succeed empty: "
            + _git_discovery_summary(discovery)
        )

    proc = _git_discovery_cli(env, "--base", base)
    summary = (
        _git_discovery_summary(proc)
        + "; git_failure="
        + _git_discovery_summary(failed_discovery)
    )
    assert proc.returncode != 0, "Incomplete Git discovery must fail the CLI: " + summary
    assert base in proc.stdout + proc.stderr, "Preserve the failed Git input in diagnostics: " + summary
    try:
        plan = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # A nonzero exit with stderr diagnostics is also a valid error surface.
        pass
    else:
        assert isinstance(plan, dict) and plan.get("ok") is False, (
            "An incomplete discovery must not emit a successful JSON plan: " + summary
        )
