from __future__ import annotations

from types import ModuleType
from typing import Any

from src.application.agent_tools import (
    analysis,
    candidate,
    close_advice,
    config,
    daily_brief,
    diagnostics,
    materialization,
    notification_perception,
    notifications,
    positions,
    portfolio,
    quality,
    runtime,
)
from src.application.agent_tools.base import AgentTool
from src.application.agent_tools.permissions import write_tools_enabled_from_env as _write_tools_enabled_from_env

AgentToolEntry = AgentTool

AGENT_TOOL_MODULES: tuple[ModuleType, ...] = (
    diagnostics,
    daily_brief,
    runtime,
    config,
    analysis,
    materialization,
    candidate,
    positions,
    portfolio,
    quality,
    close_advice,
    notifications,
    notification_perception,
)


def _module_tools(module: ModuleType) -> tuple[AgentTool, ...]:
    tools = getattr(module, "TOOLS", ())
    return tuple(tools)


def _collect_tool_definitions() -> tuple[AgentTool, ...]:
    return tuple(
        definition
        for module in AGENT_TOOL_MODULES
        for definition in _module_tools(module)
    )


AGENT_TOOL_DEFINITIONS: tuple[AgentTool, ...] = _collect_tool_definitions()


def _registry_by_name() -> dict[str, AgentTool]:
    registry: dict[str, AgentTool] = {}
    for definition in AGENT_TOOL_DEFINITIONS:
        if not definition.enabled:
            continue
        if definition.name in registry:
            raise RuntimeError(f"duplicate agent tool definition: {definition.name}")
        registry[definition.name] = definition
    return registry


AGENT_TOOL_REGISTRY: dict[str, AgentTool] = _registry_by_name()
RECOMMENDED_FLOW: tuple[str, ...] = ("healthcheck", "scan_opportunities", "get_close_advice")


def tool_names() -> tuple[str, ...]:
    return tuple(
        definition.name
        for definition in AGENT_TOOL_DEFINITIONS
        if definition.enabled
    )


def get_tool_definition(name: str) -> AgentTool | None:
    return AGENT_TOOL_REGISTRY.get(str(name or "").strip())


def pure_read_tool_names() -> frozenset[str]:
    return frozenset(
        definition.name
        for definition in AGENT_TOOL_DEFINITIONS
        if definition.enabled and definition.is_pure_read()
    )


def pure_read_toolsets() -> dict[str, tuple[str, ...]]:
    return {
        module.__name__.rsplit(".", 1)[-1]: tuple(
            definition.name
            for definition in _module_tools(module)
            if definition.enabled and definition.is_pure_read()
        )
        for module in AGENT_TOOL_MODULES
        if any(definition.enabled and definition.is_pure_read() for definition in _module_tools(module))
    }


def build_agent_spec(*, write_tools_enabled: bool | None = None) -> dict[str, Any]:
    if write_tools_enabled is None:
        write_tools_enabled = _write_tools_enabled_from_env()
    return {
        "schema_version": "1.0",
        "name": "options-monitor-local-tools",
        "description": "Local Tool Gateway tools for options-monitor. Read-first by default; write tools require explicit enablement and confirmation.",
        "launcher": {
            "command": ["./om-agent", "run", "--tool", "<tool-name>", "--input-json", "<json>"],
            "add_account_command": ["./om-agent", "add-account", "--market", "us|hk", "--account-label", "<label>", "--account-type", "futu|external_holdings", "--dry-run"],
            "edit_account_command": ["./om-agent", "edit-account", "--market", "us|hk", "--account-label", "<label>", "--dry-run"],
            "remove_account_command": ["./om-agent", "remove-account", "--market", "us|hk", "--account-label", "<label>", "--dry-run"],
        },
        "config": {
            "output_dir_env": "OM_OUTPUT_DIR",
            "write_tools_env": "OM_AGENT_ENABLE_WRITE_TOOLS",
            "service_profile_name": "service.profile.json",
        },
        "defaults": {
            "write_tools_enabled": bool(write_tools_enabled),
            "remote_hosted": False,
            "auto_trade": False,
        },
        "tools": [
            definition.to_manifest()
            for definition in AGENT_TOOL_DEFINITIONS
            if definition.enabled
        ],
        "recommended_flow": list(RECOMMENDED_FLOW),
    }


__all__ = [
    "AGENT_TOOL_DEFINITIONS",
    "AGENT_TOOL_MODULES",
    "AGENT_TOOL_REGISTRY",
    "AgentToolEntry",
    "RECOMMENDED_FLOW",
    "build_agent_spec",
    "get_tool_definition",
    "pure_read_tool_names",
    "pure_read_toolsets",
    "tool_names",
]
