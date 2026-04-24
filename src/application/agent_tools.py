from __future__ import annotations

from typing import Any

from scripts.agent_plugin.main import build_spec as _build_spec
from scripts.agent_plugin.main import run_tool as _run_tool


def build_agent_spec() -> dict[str, Any]:
    return _build_spec()


def run_agent_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_tool(str(tool_name or "").strip(), dict(payload or {}))

