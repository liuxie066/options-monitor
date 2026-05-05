from __future__ import annotations

from typing import Any

from src.application.agent_tool_registry import build_agent_spec as _build_agent_spec
from src.application.tool_execution import execute_tool


def build_agent_spec() -> dict[str, Any]:
    return _build_agent_spec()


def run_agent_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return execute_tool(str(tool_name or "").strip(), dict(payload or {}))
