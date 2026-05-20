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
    audit_db: str | None = None,
    profile_path: str | None = None,
    include_service_status: bool = False,
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
    if audit_db:
        payload["audit_db"] = str(audit_db)
    if profile_path:
        payload["profile_path"] = str(profile_path)
    if include_service_status:
        payload["include_service_status"] = True
    return execute_tool("healthcheck", payload)
