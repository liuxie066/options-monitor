from __future__ import annotations

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
    assert plan["commands"][0] == "python3 scripts/release_check.py --tag v1.2.183"
    assert "git diff --check" in plan["commands"]
    assert "python3 -m pytest tests/test_event_prefetch.py tests/test_event_source_futu.py tests/test_event_risk_warn.py" in plan["commands"]
    assert (
        "python3 -m pytest tests/test_service_deploy.py tests/test_version_check.py tests/test_install_script.py "
        "tests/test_release_test_plan.py"
    ) in plan["commands"]
    assert "python3 scripts/generate_dependency_graph.py --check" in plan["commands"]
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
    assert plan["commands"][0] == "python3 scripts/release_check.py --tag v1.2.183"
    assert plan["commands"][-1] == "python3 -m pytest"


def test_release_test_plan_maps_agent_reliability_changes_to_p2_gate() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(
        changed_files=[
            "src/application/assistant/coverage_verifier.py",
            "src/application/agent_tools/analysis.py",
            "tests/fixtures/assistant_agent_eval.jsonl",
        ],
        mode="standard",
        version="1.2.184",
    )

    assert plan["risk"] == "standard"
    assert plan["requires_full_pytest"] is False
    assert {rule["name"] for rule in plan["matched_rules"]} >= {"agent_reliability", "dependency_graph"}
    assert (
        "python3 -m pytest tests/test_assistant_agent_eval.py::test_assistant_agent_eval_fixture_covers_p2_agent_eval_gap_groups "
        "tests/test_assistant_evidence_session.py::test_format_assistant_trace_route_samples_from_fixture"
    ) in plan["commands"]
    assert (
        "python3 -m pytest tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py "
        "tests/test_assistant_runtime.py tests/test_analysis_tools.py"
    ) in plan["commands"]
    assert "python3 -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py" in plan["commands"]


def test_release_test_plan_full_mode_always_adds_full_pytest() -> None:
    from src.application.release_test_plan import build_release_test_plan

    plan = build_release_test_plan(changed_files=["docs/RELEASE_PROCESS.md"], mode="full")

    assert plan["mode"] == "full"
    assert plan["risk"] == "full"
    assert plan["requires_full_pytest"] is True
    assert plan["commands"][-1] == "python3 -m pytest"


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
