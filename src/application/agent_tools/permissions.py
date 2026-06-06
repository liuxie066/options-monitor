from __future__ import annotations

from typing import Any

from src.application.agent_tool_config import write_tools_enabled as _write_tools_enabled_from_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import AgentTool


def write_tools_enabled_from_env() -> bool:
    return _write_tools_enabled_from_config()


def tool_write_requested(tool: AgentTool, payload: dict[str, Any]) -> bool:
    return tool.is_write_requested(payload)


def write_gate_error(tool: AgentTool, payload: dict[str, Any]) -> AgentToolError | None:
    if not tool_write_requested(tool, payload):
        return None
    if not write_tools_enabled_from_env():
        return AgentToolError(
            code="PERMISSION_DENIED",
            message=f"{tool.name} write mode is disabled",
            hint="Set OM_AGENT_ENABLE_WRITE_TOOLS=true and pass confirm=true for non-dry-run writes.",
        )
    if tool.requires_confirm and not bool(payload.get("confirm", False) or payload.get("yes", False)):
        return AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message=f"confirm=true is required for {tool.name} non-dry-run writes",
            hint="Run the tool in dry-run/preview mode first, then retry with confirm=true or yes=true only when the write is intended.",
        )
    return None


__all__ = ["tool_write_requested", "write_gate_error", "write_tools_enabled_from_env"]
