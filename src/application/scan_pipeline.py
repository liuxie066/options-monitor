from __future__ import annotations

from typing import Any

from src.application.agent_tools import run_agent_tool


def run_scan(
    *,
    config_key: str | None = None,
    config_path: str | None = None,
    symbols: list[str] | None = None,
    top_n: int | None = None,
    no_context: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if config_key:
        payload["config_key"] = str(config_key)
    if config_path:
        payload["config_path"] = str(config_path)
    if symbols:
        payload["symbols"] = list(symbols)
    if top_n is not None:
        payload["top_n"] = int(top_n)
    if no_context:
        payload["no_context"] = True
    return run_agent_tool("scan_opportunities", payload)

