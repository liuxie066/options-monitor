from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.operations_impl import version_check_tool, version_update_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


_RUNTIME_RUNS_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "runtime_runs.output.v1",
    "canonical_renderer": "runtime_runs",
    "source_label": "OM 本地 output_runs",
    "guard_profile": "runtime_artifacts",
    "primary_rows": "runs",
    "fact_fields": [
        "summary.limit",
        "summary.total_runs",
        "runs[].run_id",
        "runs[].status",
        "runs[].started_at",
        "runs[].path",
    ],
}

_RUNTIME_LOGS_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "runtime_logs.output.v1",
    "canonical_renderer": "runtime_logs",
    "source_label": "OM 本地 runtime logs",
    "guard_profile": "runtime_artifacts",
    "primary_rows": "files",
    "fact_fields": [
        "summary.kind",
        "summary.lines",
        "summary.existing_file_count",
        "selected_run.run_id",
        "files[].path",
        "files[].exists",
        "files[].tail_line_count",
    ],
}


def _mask_path_str(ctx: AgentToolContext, value: Any) -> str:
    return ctx.mask_path(value) or "..."


def _version_check_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return version_check_tool(
        payload,
        check_version_update=ctx.check_version_update,
        repo_base=ctx.repo_base,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


def _version_update_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return version_update_tool(
        payload,
        update_local_version=ctx.update_local_version,
        repo_base=ctx.repo_base,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


def _version_update_write_requested(payload: dict[str, Any]) -> bool:
    return bool(payload.get("apply", False))


def _runtime_runs_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    _reject_removed_payload_alias(payload, alias="openclaw_profile_path", replacement="profile_path")
    data = ctx.collect_runtime_runs(
        repo_root=ctx.repo_base(),
        runs_root=payload.get("runs_root"),
        profile_path=payload.get("profile_path"),
        limit=payload.get("limit") or 10,
        run_id=payload.get("run_id"),
        run_dir=payload.get("run_dir"),
        scanned_only=bool(payload.get("scanned_only")),
    )
    return data, [], {"runs_root": ctx.mask_path(data.get("runs_root"))}


def _runtime_logs_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    _reject_removed_payload_alias(payload, alias="openclaw_profile_path", replacement="profile_path")
    _reject_removed_payload_alias(payload, alias="file", replacement="log_file")
    data = ctx.collect_runtime_logs(
        repo_root=ctx.repo_base(),
        runs_root=payload.get("runs_root"),
        logs_root=payload.get("logs_root"),
        profile_path=payload.get("profile_path"),
        run_id=payload.get("run_id"),
        run_dir=payload.get("run_dir"),
        kind=str(payload.get("kind") or "all"),
        lines=int(payload.get("lines") or 50),
        log_file=payload.get("log_file"),
    )
    return data, [], {
        "runs_root": ctx.mask_path(data.get("runs_root")),
        "logs_root": ctx.mask_path(data.get("logs_root")),
    }


def _reject_removed_payload_alias(payload: dict[str, Any], *, alias: str, replacement: str) -> None:
    if alias in payload and str(payload.get(alias) or "").strip():
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"{alias} has been removed; use {replacement}",
        )


VERSION_CHECK_TOOL = build_agent_tool(
    name="version_check",
    description="Check local VERSION against git release tags without running any monitor workflow.",
    requires=("git_remote",),
    capabilities=("version_check", "read_only"),
    input_schema={
        "remote_name": "optional git remote name; defaults to origin",
    },
    handler=_version_check_tool,
    pure_read=True,
    safe_default_input={"remote_name": "origin"},
    examples=({"input": {"remote_name": "origin"}},),
)

VERSION_UPDATE_TOOL = build_agent_tool(
    name="version_update",
    description="Preview or update local VERSION. Does not create git tags, commit, push, or run release workflows.",
    requires=("local_repo",),
    capabilities=("version_update", "local_write", "release_metadata"),
    side_effects=("writes_VERSION",),
    input_schema={
        "target_version": "optional explicit semver target such as 1.2.3",
        "bump": "optional major|minor|patch; defaults to patch when no version is provided",
        "apply": "optional bool; default false previews only",
        "confirm": "required true when apply=true",
        "allow_downgrade": "optional bool; default false rejects lower target versions",
    },
    handler=_version_update_tool,
    read_only=False,
    risk_level="local_write",
    requires_confirm=True,
    safe_default_input={"bump": "patch", "apply": False},
    write_request_predicate=_version_update_write_requested,
    examples=(
        {"input": {"bump": "patch", "apply": False}},
        {"input": {"target_version": "1.2.3", "apply": True, "confirm": True}},
    ),
)

RUNTIME_RUNS_TOOL = build_agent_tool(
    name="runtime_runs",
    description=(
        "List and inspect local runtime run snapshots from output_runs without running pipelines "
        "or sending notifications."
    ),
    requires=("runtime_artifacts",),
    capabilities=("runs", "read_only", "runtime_artifacts"),
    input_schema={
        "runs_root": "optional run history root; defaults to output_runs",
        "profile_path": "optional service.profile.json path",
        "limit": "optional number of recent runs to return; defaults to 10; <=0 returns all",
        "run_id": "optional output_runs run id to inspect",
        "run_dir": "optional explicit run directory to inspect",
        "scanned_only": "optional bool; only return runs that recorded ran_scan=true",
    },
    handler=_runtime_runs_tool,
    pure_read=True,
    safe_default_input={"limit": 10},
    examples=(
        {"input": {"limit": 10}},
        {"input": {"run_id": "20260515T182459Z-474761"}},
    ),
    output_contract=_RUNTIME_RUNS_OUTPUT_CONTRACT,
)

RUNTIME_LOGS_TOOL = build_agent_tool(
    name="runtime_logs",
    description=(
        "Tail local runtime run audit files or service logs without running pipelines or sending notifications."
    ),
    requires=("runtime_artifacts",),
    capabilities=("logs", "audit_tail", "read_only", "runtime_artifacts"),
    input_schema={
        "runs_root": "optional run history root; defaults to output_runs",
        "logs_root": "optional service log root; defaults to logs or profile runtime_root/logs",
        "profile_path": "optional service.profile.json path",
        "run_id": "optional output_runs run id to inspect",
        "run_dir": "optional explicit run directory to inspect",
        "kind": "optional all|audit|tool|tick|service; defaults all",
        "lines": "optional number of tail lines; defaults 50",
        "log_file": "optional explicit log file path",
    },
    handler=_runtime_logs_tool,
    pure_read=True,
    safe_default_input={"kind": "all", "lines": 50},
    examples=(
        {"input": {"run_id": "20260515T182459Z-474761", "kind": "tool", "lines": 20}},
        {"input": {"kind": "service", "lines": 50}},
    ),
    output_contract=_RUNTIME_LOGS_OUTPUT_CONTRACT,
)

TOOLS: tuple[AgentTool, ...] = (
    VERSION_CHECK_TOOL,
    VERSION_UPDATE_TOOL,
    RUNTIME_RUNS_TOOL,
    RUNTIME_LOGS_TOOL,
)


__all__ = ["RUNTIME_LOGS_TOOL", "RUNTIME_RUNS_TOOL", "TOOLS", "VERSION_CHECK_TOOL", "VERSION_UPDATE_TOOL"]
