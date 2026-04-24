from __future__ import annotations

from typing import Any

from src.application.agent_tools import build_agent_spec, run_agent_tool


def build_tool_manifest() -> dict[str, Any]:
    return build_agent_spec()


def execute_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_agent_tool(tool_name, payload)

