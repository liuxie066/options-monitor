from __future__ import annotations

import re
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.operations_impl import version_check_tool, version_update_tool
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.version_check import check_version_update
from src.application.runtime_logs_cli import RUN_LOG_FILES, collect_runtime_logs
from src.application.runtime_runs_cli import collect_runtime_runs
from src.application.agent_tool_contracts import mask_path
from src.application.agent_tool_config import repo_base
from src.application.release_target import VERSION_RE
from src.application.version_check import update_local_version


_VERSION_CHECK_OUTPUT_CONTRACT: dict[str, Any] = {
    "evidence_type": "diagnostic",
    "bounded_projection": "contract_fields",
    "coverage": "point",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
    "schema_version": "version_check.output.v1",
    "source_label": "local VERSION and remote git release tags",
    "result_shape": "scalar",
    "fact_fields": [
        "ok",
        "current_version",
        "latest_version",
        "update_available",
        "release_tag",
        "remote_name",
        "checked_at",
        "message",
        "error",
    ],
    "freshness_fields": ["checked_at"],
    "missing_data_fields": ["latest_version", "release_tag", "error"],
}


_VERSION_UPDATE_MANUAL_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "version_update.output.v1",
    "source_label": "local VERSION",
    "result_shape": "scalar",
    "fact_fields": [
        "mode",
        "current_version",
        "target_version",
        "changed",
        "would_change",
        "version_path",
        "message",
    ],
    "missing_data_fields": [],
}

_VERSION_UPDATE_AUTO_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "release_version_recommendation.v1",
    "source_label": "CHANGELOG.md Unreleased, local git evidence, and remote stable tags",
    "result_shape": "release_version_recommendation",
    "status_values": ["recommended", "blocked", "needs_input", "stale", "applied", "already_at_target"],
    "fact_fields": [
        "status",
        "reason_code",
        "base.version",
        "base.tag",
        "base.remote_name",
        "workspace.head",
        "workspace.changed_files",
        "recommendation.bump",
        "recommendation.target_version",
        "evidence",
        "review_flags",
        "recommendation_digest",
        "write.changed",
        "write.already_at_target",
    ],
    "freshness_fields": ["recommendation_digest", "base.remote_commit_sha", "workspace.head"],
    "missing_data_fields": ["reason_code", "recommendation_digest"],
}

_VERSION_UPDATE_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "version_update.output.v2",
    "payload_dependent": True,
    "variants": {
        "manual": _VERSION_UPDATE_MANUAL_OUTPUT_CONTRACT,
        "auto": _VERSION_UPDATE_AUTO_OUTPUT_CONTRACT,
    },
}


_RUNTIME_RUNS_OUTPUT_CONTRACT: dict[str, Any] = {
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
    "schema_version": "runtime_runs.output.v1",
    "source_label": "OM 本地 output_runs",
    "primary_rows": "runs",
    "fact_fields": [
        "summary.limit",
        "summary.total_count",
        "summary.returned_count",
        "summary.requested_found",
        "runs[].run_id",
        "runs[].status",
        "runs[].mtime_utc",
        "runs[].ran_scan",
        "runs[].sent",
        "runs[].path",
    ],
    "freshness_fields": ["runs[].mtime_utc", "selected_run.mtime_utc"],
    "missing_data_fields": ["summary.runs_root_exists", "summary.requested_found"],
    "model_preview_fields": ["summary", "selected_run", "runs"],
}

_RUNTIME_LOGS_OUTPUT_CONTRACT: dict[str, Any] = {
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
    "schema_version": "runtime_logs.output.v2",
    "source_label": "OM 本地 runtime logs",
    "primary_rows": "files",
    "fact_fields": [
        "summary.kind",
        "summary.lines",
        "summary.existing_file_count",
        "selected_run.run_id",
        "files[].kind",
        "files[].exists",
        "files[].size_bytes",
        "files[].tail_line_count",
        "files[].tail_truncated",
        "files[].error_code",
    ],
    "missing_data_fields": ["summary.requested_run_found", "files[].exists", "files[].error_code"],
    "model_preview_fields": ["summary", "selected_run", "files"],
    "model_value_fields": [
        "summary.kind",
        "summary.lines",
        "summary.lines_capped",
        "summary.requested_run_found",
        "summary.file_count",
        "summary.existing_file_count",
        "selected_run.run_id",
        "selected_run.exists",
        "files[].kind",
        "files[].exists",
        "files[].size_bytes",
        "files[].tail_line_count",
        "files[].tail_truncated",
        "files[].error_code",
    ],
}


def _mask_path_str(value: Any) -> str:
    return mask_path(value) or "..."


def _version_check_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return version_check_tool(
        payload,
        check_version_update=check_version_update,
        repo_base=repo_base,
        mask_path=lambda value: _mask_path_str(value),
    )


def _version_update_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return version_update_tool(
        payload,
        update_local_version=update_local_version,
        repo_base=repo_base,
        mask_path=lambda value: _mask_path_str(value),
    )


def _version_update_write_requested(payload: dict[str, Any]) -> bool:
    return bool(payload.get("apply", False))


def _version_update_output_contract(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("bump") or "").strip().lower() == "auto":
        return _VERSION_UPDATE_AUTO_OUTPUT_CONTRACT
    return _VERSION_UPDATE_MANUAL_OUTPUT_CONTRACT


def _validate_version_update_input(payload: dict[str, Any]) -> None:
    bump = str(payload.get("bump") or "").strip().lower()
    target = str(payload.get("target_version") or "").strip()
    if bump and target:
        raise AgentToolError(code="INPUT_ERROR", message="provide either target_version or bump, not both")
    if bump != "auto":
        return
    if target:
        raise AgentToolError(code="INPUT_ERROR", message="target_version cannot be combined with bump=auto")
    if not bool(payload.get("apply", False)):
        return
    required = ("recommendation_digest", "expected_base_version", "expected_target_version")
    missing = [name for name in required if not str(payload.get(name) or "").strip()]
    if missing:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"bump=auto apply requires: {', '.join(missing)}",
            hint="Run bump=auto with apply=false first, show the recommendation, then reuse its digest/base/target after confirmation.",
        )
    digest = str(payload.get("recommendation_digest") or "").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise AgentToolError(code="INPUT_ERROR", message="recommendation_digest must be sha256:<64 lowercase hex chars>")
    for name in ("expected_base_version", "expected_target_version"):
        value = str(payload.get(name) or "").strip()
        if not VERSION_RE.fullmatch(value):
            raise AgentToolError(code="INPUT_ERROR", message=f"{name} must be a valid semver value")


def _runtime_runs_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    _reject_removed_payload_alias(payload, alias="openclaw_profile_path", replacement="profile_path")
    data = collect_runtime_runs(
        repo_root=repo_base(),
        runs_root=payload.get("runs_root"),
        profile_path=payload.get("profile_path"),
        limit=payload.get("limit") or 10,
        run_id=payload.get("run_id"),
        run_dir=payload.get("run_dir"),
        scanned_only=bool(payload.get("scanned_only")),
    )
    return data, [], {"runs_root": mask_path(data.get("runs_root"))}


def _runtime_logs_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    _reject_removed_payload_alias(payload, alias="openclaw_profile_path", replacement="profile_path")
    _reject_removed_payload_alias(payload, alias="file", replacement="log_file")
    data = collect_runtime_logs(
        repo_root=repo_base(),
        runs_root=payload.get("runs_root"),
        logs_root=payload.get("logs_root"),
        profile_path=payload.get("profile_path"),
        run_id=payload.get("run_id"),
        run_dir=payload.get("run_dir"),
        kind=str(payload.get("kind") or "all"),
        lines=int(payload.get("lines") or 50),
        log_file=payload.get("log_file"),
    )
    return _public_runtime_logs(data), [], {
        "runs_root": mask_path(data.get("runs_root")),
        "logs_root": mask_path(data.get("logs_root")),
    }


def _public_runtime_logs(data: dict[str, Any]) -> dict[str, Any]:
    raw_summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    raw_selected = data.get("selected_run") if isinstance(data.get("selected_run"), dict) else {}
    raw_files = data.get("files") if isinstance(data.get("files"), list) else []
    files: list[dict[str, Any]] = []
    for raw_item in raw_files:
        item = raw_item if isinstance(raw_item, dict) else {}
        files.append(
            {
                "kind": _public_log_kind(item.get("path")),
                "exists": bool(item.get("exists")),
                "size_bytes": item.get("size_bytes") if isinstance(item.get("size_bytes"), int) else None,
                "tail_line_count": int(item.get("tail_line_count") or 0),
                "tail_truncated": bool(item.get("tail_truncated")),
                **(
                    {"error_code": str(item.get("error_code"))}
                    if item.get("error_code")
                    else {}
                ),
            }
        )
    selected_run = None
    if raw_selected:
        selected_run = {
            "run_id": str(raw_selected.get("run_id") or ""),
            "exists": bool(raw_selected.get("exists")),
        }
    return {
        "schema_version": "runtime_logs-public.v2",
        "summary": {
            "ok": bool(raw_summary.get("ok")),
            "kind": str(raw_summary.get("kind") or "all"),
            "lines": int(raw_summary.get("lines") or 0),
            "lines_capped": bool(raw_summary.get("lines_capped")),
            "requested_run": bool(raw_summary.get("requested_run")),
            "requested_run_found": raw_summary.get("requested_run_found"),
            "file_count": len(files),
            "existing_file_count": sum(1 for item in files if item["exists"]),
        },
        "selected_run": selected_run,
        "files": files,
    }


def _public_log_kind(value: Any) -> str:
    name = str(value or "").rsplit("/", 1)[-1]
    by_name = {filename: kind for kind, filename in RUN_LOG_FILES.items()}
    return by_name.get(name, "service")


def _reject_removed_payload_alias(payload: dict[str, Any], *, alias: str, replacement: str) -> None:
    if alias in payload and str(payload.get(alias) or "").strip():
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"{alias} has been removed; use {replacement}",
        )


VERSION_CHECK_TOOL = build_agent_tool(
    name="version_check",
    catalog_summary="读取当前版本与可用版本信息。",
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
    output_contract=_VERSION_CHECK_OUTPUT_CONTRACT,
)

VERSION_UPDATE_TOOL = build_agent_tool(
    name="version_update",
    description=(
        "Preview or update local VERSION. bump=auto recommends major|minor|patch from CHANGELOG.md Unreleased "
        "(Breaking Changes, New Features, Improvements, Bug Fixes) and remote stable tags. Auto mode returns "
        "needs_input when compatibility-sensitive files changed without a declared Breaking Change; use an "
        "explicit minor/patch bump only after confirming compatibility. Remote access is used only in auto mode. "
        "Does not create git tags, commit, push, or run release workflows."
    ),
    requires=("local_repo", "git_remote"),
    capabilities=("version_update", "local_write", "release_metadata"),
    side_effects=("writes_VERSION",),
    input_schema={
        "target_version": "optional explicit semver target such as 1.2.3",
        "bump": {
            "type": "string",
            "enum": ["major", "minor", "patch", "auto"],
            "description": "optional bump kind; defaults to patch; auto reads remote tags and CHANGELOG.md Unreleased",
        },
        "remote_name": "optional git remote name for bump=auto; defaults to origin",
        "recommendation_digest": "required for apply=true with bump=auto; sha256 freshness digest from preview",
        "expected_base_version": "required semver base for apply=true with bump=auto",
        "expected_target_version": "required semver target for apply=true with bump=auto",
        "apply": "optional bool; default false previews only",
        "confirm": "required true when apply=true",
        "confirm_major": {
            "type": "boolean",
            "description": "required true in addition to confirm when apply=true crosses a MAJOR version",
        },
        "allow_downgrade": "optional bool; default false rejects lower manual target versions",
    },
    handler=_version_update_tool,
    read_only=False,
    risk_level="local_write",
    requires_confirm=True,
    safe_default_input={"bump": "patch", "apply": False},
    write_request_predicate=_version_update_write_requested,
    input_validator=_validate_version_update_input,
    output_contract=_VERSION_UPDATE_OUTPUT_CONTRACT,
    output_contract_resolver=_version_update_output_contract,
    examples=(
        {"input": {"bump": "patch", "apply": False}},
        {"input": {"bump": "auto", "apply": False, "remote_name": "origin"}},
        {"input": {"target_version": "1.2.3", "apply": True, "confirm": True}},
    ),
)

RUNTIME_RUNS_TOOL = build_agent_tool(
    name="runtime_runs",
    catalog_summary="读取历史运行记录及其状态。",
    description=(
        "List historical runtime run snapshots or inspect one run. Use after runtime_status when a specific run "
        "must be selected; this does not contain detailed service log lines."
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
    copilot_input_fields=("limit", "run_id", "scanned_only"),
)

RUNTIME_LOGS_TOOL = build_agent_tool(
    name="runtime_logs",
    catalog_summary="读取指定运行的受限日志元数据。",
    description=(
        "Read bounded, content-free audit or service log metadata for a known runtime failure. Use after "
        "runtime_status or runtime_runs identifies the relevant component or run."
    ),
    requires=("runtime_artifacts",),
    capabilities=("logs", "read_only", "runtime_artifacts"),
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
    copilot_input_fields=("run_id", "kind", "lines"),
)

TOOLS: tuple[AgentTool, ...] = (
    VERSION_CHECK_TOOL,
    VERSION_UPDATE_TOOL,
    RUNTIME_RUNS_TOOL,
    RUNTIME_LOGS_TOOL,
)


__all__ = ["RUNTIME_LOGS_TOOL", "RUNTIME_RUNS_TOOL", "TOOLS", "VERSION_CHECK_TOOL", "VERSION_UPDATE_TOOL"]
