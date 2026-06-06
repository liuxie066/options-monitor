from __future__ import annotations

from typing import Any

from src.application.agent_tool_operations import config_validate_tool, scheduler_status_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


def _mask_path_str(ctx: AgentToolContext, value: Any) -> str:
    return ctx.mask_path(value) or "..."


def _config_validate_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return config_validate_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        validate_runtime_config=ctx.validate_runtime_config,
        accounts_from_config=ctx.accounts_from_config,
        resolve_watchlist_config=ctx.resolve_watchlist_config,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


def _scheduler_status_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return scheduler_status_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        read_state=ctx.read_scheduler_state,
        decide=ctx.scheduler_decide,
        repo_base=ctx.repo_base,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


CONFIG_VALIDATE_TOOL = build_agent_tool(
    name="config_validate",
    description="Validate runtime config only, without OpenD checks or pipeline execution.",
    requires=("runtime_config",),
    capabilities=("config_validate", "read_only"),
    input_schema={
        "config_key": "us|hk (optional when config_path is set)",
        "config_path": "absolute or relative JSON config path",
        "allow_empty_symbols": "optional bool for first-time config scaffolds",
    },
    handler=_config_validate_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us"}},),
)

SCHEDULER_STATUS_TOOL = build_agent_tool(
    name="scheduler_status",
    description="Return scheduler decision and existing scheduler state without marking scan/notify state or running pipelines.",
    requires=("runtime_config",),
    capabilities=("scheduler_status", "read_only"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "state_dir": "optional state dir; defaults to output_shared/state",
        "state": "optional explicit scheduler state file",
        "schedule_key": "optional schedule key; defaults to schedule",
        "account": "optional account label",
        "force": "optional bool to preview force-mode scheduler decision",
    },
    handler=_scheduler_status_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us", "account": "lx"}},),
)

TOOLS: tuple[AgentTool, ...] = (
    CONFIG_VALIDATE_TOOL,
    SCHEDULER_STATUS_TOOL,
)


__all__ = ["CONFIG_VALIDATE_TOOL", "SCHEDULER_STATUS_TOOL", "TOOLS"]
