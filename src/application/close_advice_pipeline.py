from __future__ import annotations

from typing import Any

from src.application.agent_tools import run_agent_tool


def run_close_advice(
    *,
    config_key: str | None = None,
    config_path: str | None = None,
    account: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if config_key:
        payload["config_key"] = str(config_key)
    if config_path:
        payload["config_path"] = str(config_path)
    if account:
        payload["account"] = str(account)
    if output_dir:
        payload["output_dir"] = str(output_dir)
    return run_agent_tool("get_close_advice", payload)

