from __future__ import annotations

import json
from typing import Any

from scripts.agent_plugin.config import write_tools_enabled
from src.application.agent_tool_registry import build_agent_spec
from src.application.tool_execution import execute_tool


def build_spec() -> dict[str, Any]:
    return build_agent_spec(write_tools_enabled=write_tools_enabled())


def run_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return execute_tool(tool_name, payload)


def dumps_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
