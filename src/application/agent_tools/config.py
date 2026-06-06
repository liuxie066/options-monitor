from __future__ import annotations

from typing import Any

from src.application.agent_tool_operations import config_validate_tool, scheduler_status_tool
from src.application.agent_tool_symbols import manage_symbols_tool
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


def _manage_symbols_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return manage_symbols_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        deepcopy_fn=ctx.deepcopy_value,
        write_tools_enabled=ctx.write_tools_enabled,
        apply_symbol_mutation_fn=lambda cfg, tool_payload: ctx.apply_symbol_mutation(
            cfg,
            tool_payload,
            normalize_accounts=ctx.normalize_accounts,
            resolve_watchlist_config=ctx.resolve_watchlist_config,
        ),
        validate_runtime_config=ctx.validate_runtime_config,
        list_symbol_rows_fn=lambda cfg: ctx.list_symbol_rows(
            cfg,
            resolve_watchlist_config=ctx.resolve_watchlist_config,
            normalize_accounts=ctx.normalize_accounts,
        ),
        write_json_atomic=ctx.write_json_atomic,
        mask_path=ctx.mask_path,
    )


def _manage_symbols_write_requested(payload: dict[str, Any]) -> bool:
    action = str(payload.get("action") or "list").strip().lower()
    return action != "list" and not bool(payload.get("dry_run", False))


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

MANAGE_SYMBOLS_TOOL = build_agent_tool(
    name="manage_symbols",
    description="List or mutate symbols[] entries. Write actions require OM_AGENT_ENABLE_WRITE_TOOLS=true and confirm=true.",
    requires=("runtime_config",),
    capabilities=("config_write",),
    side_effects=("writes_runtime_config",),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "action": "list|add|edit|remove",
        "symbol": "required for add/edit/remove",
        "set": "edit-only object of dot-path -> value",
        "dry_run": "optional bool",
        "confirm": "required true for non-dry-run writes",
    },
    handler=_manage_symbols_tool,
    read_only=False,
    risk_level="local_write",
    requires_confirm=True,
    requires_env=("OM_AGENT_ENABLE_WRITE_TOOLS=true for non-dry-run writes",),
    safe_default_input={"action": "list"},
    write_request_predicate=_manage_symbols_write_requested,
    examples=(
        {"input": {"config_key": "us", "action": "list"}},
        {"input": {"config_key": "us", "action": "add", "symbol": "NVDA", "dry_run": True}},
    ),
)

TOOLS: tuple[AgentTool, ...] = (
    CONFIG_VALIDATE_TOOL,
    SCHEDULER_STATUS_TOOL,
    MANAGE_SYMBOLS_TOOL,
)


__all__ = ["CONFIG_VALIDATE_TOOL", "MANAGE_SYMBOLS_TOOL", "SCHEDULER_STATUS_TOOL", "TOOLS"]
