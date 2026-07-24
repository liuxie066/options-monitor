from __future__ import annotations

import fnmatch
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RISK_ORDER = {"fast": 0, "standard": 1, "full": 2}
VALID_MODES = {"fast", "standard", "full"}


@dataclass(frozen=True)
class TestRule:
    name: str
    patterns: tuple[str, ...]
    reason: str
    commands: tuple[str, ...]
    risk: str = "standard"


TEST_RULES: tuple[TestRule, ...] = (
    TestRule(
        name="event_source",
        patterns=(
            "src/application/events/**",
            "src/application/event_risk_filter.py",
            "src/interfaces/cli/event_source_ops.py",
            "tests/test_event_source_futu.py",
        ),
        reason="event source or event-risk files changed",
        commands=(
            "./.venv/bin/python -m pytest tests/test_event_prefetch.py tests/test_event_source_futu.py tests/test_event_risk_warn.py",
        ),
    ),
    TestRule(
        name="config",
        patterns=(
            "src/application/config_*.py",
            "src/application/config_validator.py",
            "src/application/layered_config.py",
            "configs/system.json",
            "config*.json",
            "configs/examples/**",
            "tests/test_config_yaml.py",
            "tests/test_layered_config.py",
        ),
        reason="config generation or validation files changed",
        commands=(
            "./.venv/bin/python -m pytest tests/test_config_yaml.py tests/test_layered_config.py tests/test_validate_config_notifications.py",
            "./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example",
            "./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example",
            "./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run",
            "./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run",
        ),
    ),
    TestRule(
        name="agent_cli",
        patterns=(
            "src/interfaces/cli/**",
            "src/interfaces/agent/**",
            "src/application/agent_tool_*.py",
            "tests/test_agent_plugin_contract.py",
            "tests/test_agent_plugin_smoke.py",
            "tests/test_cli_operator_commands.py",
        ),
        reason="CLI or agent tool surface changed",
        commands=(
            "./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py tests/test_cli_operator_commands.py",
        ),
    ),
    TestRule(
        name="tick",
        patterns=(
            "src/application/multi_tick/**",
            "src/application/multi_account_tick.py",
            "src/application/tick_*.py",
            "src/interfaces/cli/run_ops.py",
            "tests/test_multi_tick_*.py",
            "tests/test_unified_tick_entrypoint.py",
            "tests/test_tick_cron.py",
        ),
        reason="tick orchestration files changed",
        commands=(
            "./.venv/bin/python -m pytest tests/test_multi_tick_*.py tests/test_unified_tick_entrypoint.py tests/test_tick_cron.py",
        ),
    ),
    TestRule(
        name="ledger_positions_trades",
        patterns=(
            "domain/domain/ledger/**",
            "src/application/positions/**",
            "src/application/trades/**",
            "tests/test_option_positions*.py",
            "tests/test_trade*.py",
        ),
        reason="ledger, position, or trade state files changed",
        commands=(
            "./.venv/bin/python -m pytest tests/test_option_positions*.py tests/test_trade*.py",
        ),
        risk="full",
    ),
    TestRule(
        name="service_release",
        patterns=(
            ".github/workflows/**",
            "scripts/install*",
            "scripts/release_test_plan.py",
            "src/application/release_test_plan.py",
            "src/application/release_version_recommendation.py",
            "src/application/release_target.py",
            "src/application/version_check.py",
            "src/application/service_upgrade.py",
            "src/application/service_deploy.py",
            "src/interfaces/cli/service_ops.py",
            "tests/test_release_test_plan.py",
            "tests/test_service_deploy.py",
            "tests/test_release_version_recommendation.py",
            "tests/test_version_check.py",
            "tests/test_install_script.py",
        ),
        reason="service, installer, or release-upgrade files changed",
        commands=(
            "./.venv/bin/python -m pytest tests/test_service_deploy.py tests/test_release_version_recommendation.py tests/test_version_check.py tests/test_install_script.py tests/test_release_test_plan.py",
        ),
    ),
    TestRule(
        name="assistant_runtime",
        patterns=(
            "docs/OM_COPILOT_V2_DESIGN.md",
            "docs/INBOUND_CONTROL.md",
            "src/application/assistant/**",
            "src/application/agent_tools/**",
            "tests/test_architecture_guards.py",
            "tests/test_assistant_diagnostics.py",
            "tests/test_assistant_permission_request.py",
            "tests/test_assistant_runtime.py",
            "tests/test_cli_operator_commands.py",
            "tests/test_inbound_control.py",
            "tests/test_analysis_tools.py",
            "tests/test_agent_plugin_contract.py",
            "tests/test_agent_plugin_smoke.py",
            "tests/test_candidate_filter_trace.py",
        ),
        reason="Assistant runtime, tool contract, or read surface files changed",
        commands=(
            "./.venv/bin/python -m pytest tests/test_assistant_runtime.py tests/test_inbound_control.py "
            "tests/test_assistant_permission_request.py tests/test_cli_operator_commands.py "
            "tests/test_assistant_diagnostics.py tests/test_architecture_guards.py",
            "./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py "
            "tests/test_candidate_filter_trace.py tests/test_analysis_tools.py",
        ),
    ),
    TestRule(
        name="dependency_graph",
        patterns=(
            "scripts/generate_dependency_graph.py",
            "docs/DEPENDENCY_GRAPH.md",
            "domain/**",
            "src/**",
        ),
        reason="import graph may have changed",
        commands=("./.venv/bin/python scripts/generate_dependency_graph.py --check",),
        risk="fast",
    ),
)


def build_release_test_plan(
    *,
    changed_files: Sequence[str],
    mode: str = "standard",
    version: str | None = None,
) -> dict[str, Any]:
    selected_mode = _normalize_mode(mode)
    files = _normalize_changed_files(changed_files)
    commands = [
        _release_check_command(version),
        "git diff --check",
    ]
    reasons: list[str] = []
    matched_rules: list[dict[str, Any]] = []
    risk = "fast" if files else "standard"

    for rule in TEST_RULES:
        matched = [path for path in files if _matches_any(path, rule.patterns)]
        if not matched:
            continue
        reasons.append(rule.reason)
        matched_rules.append(
            {
                "name": rule.name,
                "risk": rule.risk,
                "reason": rule.reason,
                "matched_files": matched,
            }
        )
        risk = _max_risk(risk, rule.risk)
        commands.extend(rule.commands)

    if not reasons:
        reasons.append("no mapped high-risk source changes detected")

    requires_full_pytest = selected_mode == "full" or risk == "full"
    if requires_full_pytest:
        commands.append("./.venv/bin/python -m pytest")
        risk = "full"

    return {
        "schema_version": "1.0",
        "tool_name": "release_test_plan",
        "ok": True,
        "mode": selected_mode,
        "risk": risk,
        "reasons": reasons,
        "changed_files": files,
        "matched_rules": matched_rules,
        "commands": _dedupe(commands),
        "requires_full_pytest": requires_full_pytest,
    }


def changed_files_from_git(
    *,
    base_ref: str = "origin/main",
    run_cmd: Callable[..., Any] = subprocess.run,
    cwd: str | Path | None = None,
) -> list[str]:
    files: list[str] = []
    for command in (
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "diff", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        files.extend(_git_name_only(command, run_cmd=run_cmd, cwd=cwd))
    return _normalize_changed_files(files)


def _normalize_mode(mode: str) -> str:
    selected = str(mode or "standard").strip().lower()
    if selected not in VALID_MODES:
        raise ValueError(f"unsupported release test mode: {mode}")
    return selected


def _release_check_command(version: str | None) -> str:
    text = str(version or "").strip()
    if not text:
        return "./.venv/bin/python scripts/release_check.py"
    tag = text if text.startswith("v") else f"v{text}"
    return f"./.venv/bin/python scripts/release_check.py --tag {tag}"


def _normalize_changed_files(changed_files: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in changed_files:
        path = str(raw or "").strip().replace("\\", "/")
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return sorted(out)


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_matches(path, pattern) for pattern in patterns)


def _matches(path: str, pattern: str) -> bool:
    clean = pattern.rstrip("/")
    if clean.endswith("/**"):
        return path == clean[:-3] or path.startswith(clean[:-3] + "/")
    return fnmatch.fnmatch(path, clean)


def _max_risk(left: str, right: str) -> str:
    return left if RISK_ORDER[left] >= RISK_ORDER[right] else right


def _dedupe(commands: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for command in commands:
        if command in seen:
            continue
        seen.add(command)
        out.append(command)
    return out


def _git_name_only(command: list[str], *, run_cmd: Callable[..., Any], cwd: str | Path | None) -> list[str]:
    proc = run_cmd(
        command,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if int(getattr(proc, "returncode", 1)) != 0:
        return []
    return [line.strip() for line in str(getattr(proc, "stdout", "") or "").splitlines() if line.strip()]


__all__ = ["build_release_test_plan", "changed_files_from_git"]
