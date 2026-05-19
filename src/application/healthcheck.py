from __future__ import annotations

from typing import Any

from src.application.tool_execution import execute_tool


def run_healthcheck(
    *,
    config_key: str | None = None,
    config_path: str | None = None,
    accounts: list[str] | None = None,
    opend_telnet_host: str | None = None,
    opend_telnet_port: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if config_key:
        payload["config_key"] = str(config_key)
    if config_path:
        payload["config_path"] = str(config_path)
    if accounts:
        payload["accounts"] = list(accounts)
    if opend_telnet_host:
        payload["opend_telnet_host"] = str(opend_telnet_host)
    if opend_telnet_port:
        payload["opend_telnet_port"] = int(opend_telnet_port)
    return execute_tool("healthcheck", payload)
