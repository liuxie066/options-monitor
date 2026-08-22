from __future__ import annotations

from types import ModuleType
from typing import Any
import hashlib
import json

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


def toolset_for_definition(definition: AgentTool) -> str:
    """Return the canonical module grouping used by the Copilot catalog."""
    for module in AGENT_TOOL_MODULES:
        if definition in _module_tools(module):
            return module.__name__.rsplit(".", 1)[-1]
    raise ValueError(f"tool is not in the canonical registry: {definition.name}")


def build_compact_catalog(tool_names_value: tuple[str, ...] | list[str] | None = None) -> list[dict[str, str]]:
    """Project the existing registry into the closed model-selection catalog."""
    names = tuple(tool_names_value) if tool_names_value is not None else tool_names()
    entries: list[dict[str, str]] = []
    for name in names:
        definition = get_tool_definition(name)
        if definition is None or not definition.is_pure_read():
            raise ValueError(f"catalog tool is not an enabled pure-read tool: {name}")
        purpose = str(definition.catalog_summary or "")
        if not purpose or purpose != purpose.strip() or "\n" in purpose or len(purpose) > 240:
            raise ValueError(f"catalog_summary is missing or invalid: {name}")
        contract = definition.output_contract
        if (
            not contract
            or definition.copilot_evidence_type() == "mixed" and "evidence_type" not in contract
            or not isinstance(contract.get("bounded_projection"), str)
            or not isinstance(contract.get("coverage"), str)
            or not isinstance(contract.get("freshness"), str)
            or not isinstance(contract.get("pagination"), dict)
            or contract["pagination"].get("mode") not in {"none"}
        ):
            raise ValueError(f"output contract metadata is missing: {name}")
        entries.append({
            "name": definition.name,
            "toolset": toolset_for_definition(definition),
            "purpose": purpose,
            "access": "read",
            "evidence_type": definition.copilot_evidence_type(),
        })
    entries.sort(key=lambda item: item["name"])
    return entries


def build_catalog_snapshot(
    tool_names_value: tuple[str, ...] | list[str],
    *,
    visible_descriptions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the private, runtime-only hash material for the authorized set."""
    rows: list[dict[str, Any]] = []
    compact_by_name = {row["name"]: row for row in build_compact_catalog(tool_names_value)}
    visible_by_name = {
        str(item.get("name")): item
        for item in (visible_descriptions or [])
        if isinstance(item, dict) and item.get("name")
    }
    for name in tool_names_value:
        definition = get_tool_definition(name)
        if definition is None or not definition.is_pure_read():
            raise ValueError(f"catalog tool is not an enabled pure-read tool: {name}")
        visible = visible_by_name.get(definition.name, {})
        rows.append({
            "name": definition.name,
            "toolset": toolset_for_definition(definition),
            "description": str(visible.get("description") or definition.description),
            "input_schema": visible.get("input_schema") or definition.execution_input_json_schema(),
            "output_contract": visible.get("output_contract") or definition.output_contract,
            "access": "read",
            "purpose": compact_by_name[definition.name]["purpose"],
            "evidence_type": compact_by_name[definition.name]["evidence_type"],
        })
    return sorted(rows, key=lambda item: str(item["name"]))


def catalog_material_hash(catalog: list[dict[str, Any]], snapshot: list[dict[str, Any]]) -> str:
    material = {
        "authorized_names": sorted(str(item["name"]) for item in catalog),
        "catalog": catalog,
        "snapshot": snapshot,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    "build_compact_catalog",
    "build_catalog_snapshot",
    "catalog_material_hash",
    "get_tool_definition",
    "pure_read_tool_names",
    "pure_read_toolsets",
    "tool_names",
    "toolset_for_definition",
]
