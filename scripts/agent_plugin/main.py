from __future__ import annotations

import json
import os
from typing import Any

from scripts.agent_plugin.config import write_tools_enabled
from scripts.agent_plugin.contracts import AgentToolError, build_error_payload, build_response


def build_spec() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "options-monitor-local-tools",
        "description": "Local agent-facing tools for options-monitor. Read-first by default; write tools require explicit enablement and confirmation.",
        "launcher": {
            "command": ["./om-agent", "run", "--tool", "<tool-name>", "--input-json", "<json>"],
            "init_command": ["./om-agent", "init", "--market", "us|hk", "--futu-acc-id", "<digits>"],
            "add_account_command": ["./om-agent", "add-account", "--market", "us|hk", "--account-label", "<label>", "--account-type", "futu|external_holdings"],
            "edit_account_command": ["./om-agent", "edit-account", "--market", "us|hk", "--account-label", "<label>"],
            "remove_account_command": ["./om-agent", "remove-account", "--market", "us|hk", "--account-label", "<label>"],
        },
        "config": {
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
                "requires": ["runtime_config", "sqlite_data_config", "opend"],
                "capabilities": ["diagnostics", "read_only"],
                "side_effects": [],
                "input_schema": {
                    "config_key": "us|hk (optional when config_path is set)",
                    "config_path": "absolute or relative JSON config path",
                    "accounts": "optional list[str]",
                    "data_config": "optional explicit data config path",
                    "timeout_sec": "optional int",
                },
            },
            {
                "name": "scan_opportunities",
                "read_only": True,
                "description": "Run the symbols scan pipeline and return normalized summary rows.",
                "requires": ["runtime_config", "opend"],
                "capabilities": ["scan", "read_only"],
                "side_effects": ["writes_local_reports"],
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "data_config": "optional explicit data config path",
                    "symbols": "optional list[str] filter",
                    "top_n": "optional int",
                    "no_context": "optional bool",
                },
            },
            {
                "name": "query_cash_headroom",
                "read_only": True,
                "description": "Return sell-put cash usage and available/free cash summary.",
                "requires": ["runtime_config", "sqlite_data_config", "opend"],
                "capabilities": ["cash_query", "read_only"],
                "side_effects": ["writes_local_reports"],
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "data_config": "optional explicit data config path",
                    "pm_config": "optional deprecated alias of data_config",
                    "account": "optional account label",
                    "broker": "optional broker name, preferred public field",
                    "market": "optional deprecated alias of broker",
                    "top": "optional int",
                    "no_fx": "optional bool",
                },
            },
            {
                "name": "get_portfolio_context",
                "read_only": True,
                "description": "Fetch holdings/Futu-backed portfolio context for one account.",
                "requires": ["runtime_config", "opend"],
                "capabilities": ["portfolio_context", "read_only"],
                "side_effects": ["writes_local_cache"],
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "data_config": "optional explicit data config path",
                    "pm_config": "optional deprecated alias of data_config",
                    "account": "optional account label",
                    "broker": "optional broker name, preferred public field",
                    "market": "optional deprecated alias of broker",
                    "ttl_sec": "optional int",
                    "timeout_sec": "optional int",
                },
            },
            {
                "name": "prepare_close_advice_inputs",
                "read_only": True,
                "description": "Refresh local option positions context and required_data cache needed by close_advice.",
                "requires": ["runtime_config", "sqlite_data_config", "opend"],
                "capabilities": ["close_advice_prepare", "read_only"],
                "side_effects": ["writes_local_cache"],
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "data_config": "optional explicit data config path",
                    "pm_config": "optional deprecated alias of data_config",
                    "account": "optional account label",
                    "broker": "optional broker name, preferred public field",
                    "market": "optional deprecated alias of broker",
                    "output_dir": "optional output root; defaults to output/agent_plugin",
                    "ttl_sec": "optional int",
                    "timeout_sec": "optional int",
                },
            },
            {
                "name": "close_advice",
                "read_only": True,
                "description": "Build close-advice rows from cached option positions context and required_data quotes.",
                "requires": ["prepared_close_advice_inputs"],
                "capabilities": ["close_advice", "read_only"],
                "side_effects": ["writes_local_reports"],
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "output_dir": "optional output root; defaults to output/agent_plugin",
                    "context_path": "optional explicit option_positions_context.json path",
                    "required_data_root": "optional explicit required_data root",
                },
            },
            {
                "name": "get_close_advice",
                "read_only": True,
                "description": "One-shot close-advice entrypoint: prepare local inputs, then build close-advice output.",
                "requires": ["runtime_config", "sqlite_data_config", "opend"],
                "capabilities": ["close_advice", "read_only", "recommended_flow"],
                "side_effects": ["writes_local_cache", "writes_local_reports"],
                "input_schema": {
                    "config_key": "us|hk",
                    "config_path": "optional explicit config path",
                    "data_config": "optional explicit data config path",
                    "pm_config": "optional deprecated alias of data_config",
                    "account": "optional account label",
                    "broker": "optional broker name, preferred public field",
                    "market": "optional deprecated alias of broker",
                    "output_dir": "optional output root; defaults to output/agent_plugin",
                    "ttl_sec": "optional int",
                    "timeout_sec": "optional int",
                },
            },
            {
                "name": "manage_symbols",
                "read_only": False,
                "description": "List or mutate symbols[] entries. Write actions require OM_AGENT_ENABLE_WRITE_TOOLS=true and confirm=true.",
                "requires": ["runtime_config"],
                "capabilities": ["config_write"],
                "side_effects": ["writes_runtime_config"],
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
                "requires": ["alerts_or_changes_input"],
                "capabilities": ["notification_preview", "read_only"],
                "side_effects": [],
                "input_schema": {
                    "alerts_text": "raw alert markdown text",
                    "changes_text": "raw changes markdown text",
                    "alerts_path": "optional file path when alerts_text omitted",
                    "changes_path": "optional file path when changes_text omitted",
                    "account_label": "optional account label",
                },
            },
        ],
        "recommended_flow": [
            "healthcheck",
            "scan_opportunities",
            "get_close_advice",
        ],
    }


def run_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from scripts.agent_plugin.tools import TOOL_HANDLERS

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
