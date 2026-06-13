from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import Any

import src.application.agent_tools as agent_tools_package
from src.application.agent_tools.base import AgentTool
from src.application.agent_tools.permissions import write_tools_enabled_from_env as _write_tools_enabled_from_env

AgentToolEntry = AgentTool

_SKIP_AGENT_TOOL_MODULES: frozenset[str] = frozenset({"base"})
_PREFERRED_MODULE_ORDER: tuple[str, ...] = (
    "diagnostics",
    "runtime",
    "config",
    "analysis",
    "materialization",
    "candidate",
    "positions",
    "close_advice",
    "notifications",
)


def _module_sort_key(module: ModuleType) -> tuple[int, str]:
    name = module.__name__.rsplit(".", 1)[-1]
    try:
        preferred_index = _PREFERRED_MODULE_ORDER.index(name)
    except ValueError:
        preferred_index = len(_PREFERRED_MODULE_ORDER)
    return preferred_index, name


def _discover_tool_modules() -> tuple[ModuleType, ...]:
    package_paths = getattr(agent_tools_package, "__path__", ())
    prefix = f"{agent_tools_package.__name__}."
    modules: list[ModuleType] = []
    for module_info in pkgutil.iter_modules(package_paths):
        module_name = module_info.name
        if module_name.startswith("_") or module_name in _SKIP_AGENT_TOOL_MODULES:
            continue
        module = importlib.import_module(f"{prefix}{module_name}")
        if hasattr(module, "TOOLS"):
            modules.append(module)
    return tuple(sorted(modules, key=_module_sort_key))


AGENT_TOOL_MODULES: tuple[ModuleType, ...] = _discover_tool_modules()


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


def build_agent_spec(*, write_tools_enabled: bool | None = None) -> dict[str, Any]:
    if write_tools_enabled is None:
        write_tools_enabled = _write_tools_enabled_from_env()
    return {
        "schema_version": "1.0",
        "name": "options-monitor-local-tools",
        "description": "Local Ops Copilot tools for options-monitor. Read-first by default; write tools require explicit enablement and confirmation.",
        "launcher": {
            "command": ["./om-agent", "run", "--tool", "<tool-name>", "--input-json", "<json>"],
            "add_account_command": ["./om-agent", "add-account", "--market", "us|hk", "--account-label", "<label>", "--account-type", "futu|external_holdings", "--dry-run"],
            "edit_account_command": ["./om-agent", "edit-account", "--market", "us|hk", "--account-label", "<label>", "--dry-run"],
            "remove_account_command": ["./om-agent", "remove-account", "--market", "us|hk", "--account-label", "<label>", "--dry-run"],
        },
        "config": {
            "output_dir_env": "OM_OUTPUT_DIR",
            "write_tools_env": "OM_AGENT_ENABLE_WRITE_TOOLS",
            "openclaw_profile_names": ["openclaw.profile.json", ".openclaw-profile.json"],
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
    "tool_names",
]
