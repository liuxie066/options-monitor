from __future__ import annotations

import json
import os
from typing import Any

from scripts.agent_plugin.config import write_tools_enabled
from scripts.agent_plugin.contracts import AgentToolError, build_error_payload, build_response
from scripts.agent_plugin.tools import TOOL_HANDLERS


def build_spec() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "options-monitor-local-tools",
        "description": "Local agent-facing tools for options-monitor. Read-first by default; write tools require explicit enablement and confirmation.",
        "launcher": {
            "command": ["./om-agent", "run", "--tool", "<tool-name>", "--input-json", "<json>"],
        },
        "config": {
            "config_dir_env": "OM_CONFIG_DIR",
            "config_us_env": "OM_CONFIG_US",
            "config_hk_env": "OM_CONFIG_HK",
            "pm_config_env": "OM_PM_CONFIG",
            "output_dir_env": "OM_OUTPUT_DIR",
            "write_tools_env": "OM_AGENT_ENABLE_WRITE_TOOLS",
        },
        "defaults": {
            "write_tools_enabled": write_tools_enabled(),
            "remote_hosted": False,
            "auto_trade": False,
        },
        "tools": [
            {
                "name": "healthcheck",
                "read_only": True,
                "description": "Validate runtime config and summarize local readiness without sending notifications or writing remote data.",
                "input_schema": {
                    "config_key": "us|hk (optional when config_path is set)",
                    "config_path": "absolute or relative JSON config path",
                    "accounts": "optional list[str]",
                },
            },
            {
                "name": "scan_opportunities",
                "read_only": True,
                "description": "Run the symbols scan pipeline and return normalized summary rows.",
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "symbols": "optional list[str] filter",
                    "top_n": "optional int",
                    "no_context": "optional bool",
                },
            },
            {
                "name": "query_cash_headroom",
                "read_only": True,
                "description": "Return sell-put cash usage and available/free cash summary.",
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "account": "optional account label",
                    "market": "optional broker/market name",
                    "top": "optional int",
                    "no_fx": "optional bool",
                },
            },
            {
                "name": "get_portfolio_context",
                "read_only": True,
                "description": "Fetch holdings/Futu-backed portfolio context for one account.",
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "account": "optional account label",
                    "market": "optional broker/market name",
                    "ttl_sec": "optional int",
                    "timeout_sec": "optional int",
                },
            },
            {
                "name": "manage_symbols",
                "read_only": False,
                "description": "List or mutate symbols[] entries. Write actions require OM_AGENT_ENABLE_WRITE_TOOLS=true and confirm=true.",
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "action": "list|add|edit|remove",
                    "symbol": "required for add/edit/remove",
                    "set": "edit-only object of dot-path -> value",
                    "dry_run": "optional bool",
                    "confirm": "required true for non-dry-run writes",
                },
            },
            {
                "name": "preview_notification",
                "read_only": True,
                "description": "Build final notification text from alerts/changes without sending it.",
                "input_schema": {
                    "alerts_text": "raw alert markdown text",
                    "changes_text": "raw changes markdown text",
                    "alerts_path": "optional file path when alerts_text omitted",
                    "changes_path": "optional file path when changes_text omitted",
                    "account_label": "optional account label",
                },
            },
        ],
    }


def run_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(str(tool_name or "").strip())
    if handler is None:
        err = AgentToolError(
            code="INPUT_ERROR",
            message=f"unknown tool: {tool_name}",
            hint="Call `om-agent spec` to inspect supported tools.",
        )
        return build_response(tool_name=str(tool_name or ""), ok=False, error=build_error_payload(err))

    try:
        data, warnings, meta = handler(dict(payload or {}))
        return build_response(
            tool_name=str(tool_name),
            ok=True,
            data=data,
            warnings=warnings,
            meta=meta,
        )
    except AgentToolError as err:
        return build_response(
            tool_name=str(tool_name),
            ok=False,
            error=build_error_payload(err),
        )
    except Exception as exc:
        err = AgentToolError(
            code="INTERNAL_ERROR",
            message=f"{type(exc).__name__}: {exc}",
        )
        return build_response(
            tool_name=str(tool_name),
            ok=False,
            error=build_error_payload(err),
        )


def dumps_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
