from __future__ import annotations

from typing import Any

from src.application.agent_tools import run_agent_tool


def run_healthcheck(*, config_key: str | None = None, config_path: str | None = None, accounts: list[str] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if config_key:
        payload["config_key"] = str(config_key)
    if config_path:
        payload["config_path"] = str(config_path)
    if accounts:
        payload["accounts"] = list(accounts)
    return run_agent_tool("healthcheck", payload)

