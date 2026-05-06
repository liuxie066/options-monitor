from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from src.application.agent_tool_config import write_tools_enabled as _write_tools_enabled_from_config


@dataclass(frozen=True)
class AgentToolDefinition:
    name: str
    read_only: bool
    description: str
    requires: tuple[str, ...]
    capabilities: tuple[str, ...]
    input_schema: dict[str, str]
    side_effects: tuple[str, ...] = ()
    risk_level: str | None = None
    requires_confirm: bool = False
    requires_env: tuple[str, ...] = ()
    safe_default_input: dict[str, Any] = field(default_factory=dict)
    examples: tuple[dict[str, Any], ...] = ()

    def to_manifest(self) -> dict[str, Any]:
        side_effects = list(self.side_effects)
        return {
            "name": self.name,
            "read_only": self.read_only,
            "description": self.description,
            "requires": list(self.requires),
            "capabilities": list(self.capabilities),
            "side_effects": side_effects,
            "input_schema": dict(self.input_schema),
            "risk_level": self.risk_level or ("local_write" if side_effects else "read_only"),
            "requires_confirm": bool(self.requires_confirm),
            "requires_env": list(self.requires_env),
            "safe_default_input": dict(self.safe_default_input),
            "examples": deepcopy(list(self.examples)),
        }


AGENT_TOOL_DEFINITIONS: tuple[AgentToolDefinition, ...] = (
    AgentToolDefinition(
        name="healthcheck",
        read_only=True,
        description="Validate runtime config and summarize local readiness without sending notifications or writing remote data.",
        requires=("runtime_config", "sqlite_data_config", "opend"),
        capabilities=("diagnostics", "read_only"),
        input_schema={
            "config_key": "us|hk (optional when config_path is set)",
            "config_path": "absolute or relative JSON config path",
            "accounts": "optional list[str]",
            "data_config": "optional explicit data config path",
            "timeout_sec": "optional int",
        },
        risk_level="read_only",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us"}},),
    ),
    AgentToolDefinition(
        name="version_check",
        read_only=True,
        description="Check local VERSION against git release tags without running any monitor workflow.",
        requires=("git_remote",),
        capabilities=("version_check", "read_only"),
        input_schema={
            "remote_name": "optional git remote name; defaults to origin",
        },
        risk_level="read_only",
        safe_default_input={"remote_name": "origin"},
        examples=({"input": {"remote_name": "origin"}},),
    ),
    AgentToolDefinition(
        name="version_update",
        read_only=False,
        description="Preview or update local VERSION. Does not create git tags, commit, push, or run release workflows.",
        requires=("local_repo",),
        capabilities=("version_update", "local_write", "release_metadata"),
        side_effects=("writes_VERSION",),
        input_schema={
            "version": "optional explicit semver target such as 1.2.3",
            "target_version": "optional alias of version",
            "bump": "optional major|minor|patch; defaults to patch when no version is provided",
            "apply": "optional bool; default false previews only",
            "allow_downgrade": "optional bool; default false rejects lower target versions",
        },
        risk_level="local_write",
        requires_confirm=True,
        safe_default_input={"bump": "patch", "apply": False},
        examples=(
            {"input": {"bump": "patch", "apply": False}},
            {"input": {"version": "1.2.3", "apply": True}},
        ),
    ),
    AgentToolDefinition(
        name="config_validate",
        read_only=True,
        description="Validate runtime config only, without OpenD checks or pipeline execution.",
        requires=("runtime_config",),
        capabilities=("config_validate", "read_only"),
        input_schema={
            "config_key": "us|hk (optional when config_path is set)",
            "config_path": "absolute or relative JSON config path",
            "allow_empty_symbols": "optional bool for first-time config scaffolds",
        },
        risk_level="read_only",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us"}},),
    ),
    AgentToolDefinition(
        name="scheduler_status",
        read_only=True,
        description="Return scheduler decision and existing scheduler state without marking scan/notify state or running pipelines.",
        requires=("runtime_config",),
        capabilities=("scheduler_status", "read_only"),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "state_dir": "optional state dir; defaults to output/state",
            "state": "optional explicit scheduler state file",
            "schedule_key": "optional schedule key; defaults to schedule",
            "account": "optional account label",
            "force": "optional bool to preview force-mode scheduler decision",
        },
        risk_level="read_only",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us", "account": "lx"}},),
    ),
    AgentToolDefinition(
        name="scan_opportunities",
        read_only=True,
        description="Run the symbols scan pipeline and return normalized summary rows.",
        requires=("runtime_config", "opend"),
        capabilities=("scan", "read_only"),
        side_effects=("writes_local_reports",),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "data_config": "optional explicit data config path",
            "symbols": "optional list[str] filter",
            "top_n": "optional int",
            "no_context": "optional bool",
        },
        risk_level="local_write",
        safe_default_input={"config_key": "us", "top_n": 5},
        examples=({"input": {"config_key": "us", "top_n": 5}},),
    ),
    AgentToolDefinition(
        name="query_cash_headroom",
        read_only=True,
        description="Return sell-put cash usage and available/free cash summary.",
        requires=("runtime_config", "sqlite_data_config", "opend"),
        capabilities=("cash_query", "read_only"),
        side_effects=("writes_local_reports",),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "data_config": "optional explicit data config path",
            "account": "optional account label",
            "broker": "optional broker name, preferred public field",
            "top": "optional int",
            "no_exchange_rates": "optional bool",
        },
        risk_level="local_write",
        safe_default_input={"config_key": "us"},
        examples=(
            {"input": {"config_key": "us", "account": "lx"}},
            {"input": {"config_key": "us", "account": "sy"}},
        ),
    ),
    AgentToolDefinition(
        name="monthly_income_report",
        read_only=True,
        description="Return monthly option income statistics from local option positions without running market data or notification workflows.",
        requires=("runtime_config", "sqlite_data_config"),
        capabilities=("income_report", "option_positions", "read_only"),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "data_config": "optional explicit data config path",
            "account": "optional account label",
            "broker": "optional broker name, preferred public field",
            "month": "optional YYYY-MM filter",
            "include_rows": "optional bool; include realized and premium detail rows",
        },
        risk_level="read_only",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us", "account": "lx", "month": "2026-04"}},),
    ),
    AgentToolDefinition(
        name="option_positions_read",
        read_only=True,
        description="Read local option position lots, trade events, lot history, or projection inspection state.",
        requires=("runtime_config", "sqlite_data_config"),
        capabilities=("option_positions", "read_only", "ledger_diagnostics"),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "data_config": "optional explicit data config path",
            "action": "list|events|history|inspect",
            "broker": "optional broker name, preferred public field",
            "account": "optional account label",
            "status": "list-only open|close|all",
            "limit": "optional int, max 500",
            "exp_within_days": "list-only optional int",
            "record_id": "history/inspect selector",
            "feishu_record_id": "inspect selector",
            "symbol": "events/inspect selector",
            "option_type": "events/inspect put|call selector",
            "strike": "events/inspect numeric selector",
            "exp": "events/inspect YYYY-MM-DD selector",
        },
        risk_level="read_only",
        safe_default_input={"config_key": "us", "action": "list"},
        examples=(
            {"input": {"config_key": "us", "action": "list", "account": "lx", "status": "open"}},
            {"input": {"config_key": "us", "action": "history", "record_id": "rec_xxx"}},
        ),
    ),
    AgentToolDefinition(
        name="get_portfolio_context",
        read_only=True,
        description="Fetch holdings/Futu-backed portfolio context for one account.",
        requires=("runtime_config", "opend"),
        capabilities=("portfolio_context", "read_only"),
        side_effects=("writes_local_cache",),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "data_config": "optional explicit data config path",
            "account": "optional account label",
            "broker": "optional broker name, preferred public field",
            "ttl_sec": "optional int",
            "timeout_sec": "optional int",
        },
        risk_level="local_write",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us", "account": "lx"}},),
    ),
    AgentToolDefinition(
        name="prepare_close_advice_inputs",
        read_only=True,
        description="Refresh local option positions context and required_data cache needed by close_advice.",
        requires=("runtime_config", "sqlite_data_config", "opend"),
        capabilities=("close_advice_prepare", "read_only"),
        side_effects=("writes_local_cache",),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "data_config": "optional explicit data config path",
            "account": "optional account label",
            "broker": "optional broker name, preferred public field",
            "output_dir": "optional output root; defaults to output/agent_plugin",
            "ttl_sec": "optional int",
            "timeout_sec": "optional int",
        },
        risk_level="local_write",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us"}},),
    ),
    AgentToolDefinition(
        name="close_advice",
        read_only=True,
        description="Build close-advice rows from cached option positions context and required_data quotes.",
        requires=("prepared_close_advice_inputs",),
        capabilities=("close_advice", "read_only"),
        side_effects=("writes_local_reports",),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "output_dir": "optional output root; defaults to output/agent_plugin",
            "context_path": "optional explicit option_positions_context.json path",
            "required_data_root": "optional explicit required_data root",
        },
        risk_level="local_write",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us"}},),
    ),
    AgentToolDefinition(
        name="get_close_advice",
        read_only=True,
        description="One-shot close-advice entrypoint: prepare local inputs, then build close-advice output.",
        requires=("runtime_config", "sqlite_data_config", "opend"),
        capabilities=("close_advice", "read_only", "recommended_flow"),
        side_effects=("writes_local_cache", "writes_local_reports"),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "data_config": "optional explicit data config path",
            "account": "optional account label",
            "broker": "optional broker name, preferred public field",
            "output_dir": "optional output root; defaults to output/agent_plugin",
            "ttl_sec": "optional int",
            "timeout_sec": "optional int",
        },
        risk_level="local_write",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us"}},),
    ),
    AgentToolDefinition(
        name="manage_symbols",
        read_only=False,
        description="List or mutate symbols[] entries. Write actions require OM_AGENT_ENABLE_WRITE_TOOLS=true and confirm=true.",
        requires=("runtime_config",),
        capabilities=("config_write",),
        side_effects=("writes_runtime_config",),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "action": "list|add|edit|remove",
            "symbol": "required for add/edit/remove",
            "set": "edit-only object of dot-path -> value",
            "dry_run": "optional bool",
            "confirm": "required true for non-dry-run writes",
        },
        risk_level="local_write",
        requires_confirm=True,
        requires_env=("OM_AGENT_ENABLE_WRITE_TOOLS=true for non-dry-run writes",),
        safe_default_input={"config_key": "us", "action": "list"},
        examples=(
            {"input": {"config_key": "us", "action": "list"}},
            {"input": {"config_key": "us", "action": "add", "symbol": "NVDA", "dry_run": True}},
        ),
    ),
    AgentToolDefinition(
        name="preview_notification",
        read_only=True,
        description="Build final notification text from alerts/changes without sending it.",
        requires=("alerts_or_changes_input",),
        capabilities=("notification_preview", "read_only"),
        input_schema={
            "alerts_text": "raw alert markdown text",
            "changes_text": "raw changes markdown text",
            "alerts_path": "optional file path when alerts_text omitted",
            "changes_path": "optional file path when changes_text omitted",
            "account_label": "optional account label",
        },
        risk_level="read_only",
        safe_default_input={"alerts_text": "", "changes_text": ""},
        examples=(
            {
                "input": {
                    "alerts_path": "output/reports/symbols_alerts.txt",
                    "changes_path": "output/reports/symbols_changes.txt",
                }
            },
        ),
    ),
    AgentToolDefinition(
        name="runtime_status",
        read_only=True,
        description="Summarize existing OpenClaw/runtime output files without running pipelines or sending notifications.",
        requires=("runtime_config",),
        capabilities=("status", "read_only", "openclaw"),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "accounts": "optional list[str]",
            "report_dir": "optional report dir; defaults to output/reports",
            "state_dir": "optional legacy state dir; defaults to output/state",
            "shared_state_dir": "optional shared state dir; defaults to output_shared/state",
            "accounts_root": "optional accounts output root; defaults to output_accounts",
            "runs_root": "optional run history root; defaults to output_runs",
            "max_notification_chars": "optional int, capped at 20000",
        },
        risk_level="read_only",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us", "max_notification_chars": 2000}},),
    ),
    AgentToolDefinition(
        name="openclaw_readiness",
        read_only=True,
        description="OpenClaw-oriented readiness summary combining runtime_status, healthcheck, and local openclaw command availability.",
        requires=("runtime_config",),
        capabilities=("diagnostics", "read_only", "openclaw"),
        input_schema={
            "config_key": "us|hk",
            "config_path": "optional explicit config path",
            "accounts": "optional list[str]",
            "data_config": "optional explicit data config path for healthcheck",
            "timeout_sec": "optional int for healthcheck OpenD doctor",
            "max_notification_chars": "optional int, forwarded to runtime_status",
        },
        risk_level="read_only",
        safe_default_input={"config_key": "us"},
        examples=({"input": {"config_key": "us", "timeout_sec": 20}},),
    ),
)


def _registry_by_name() -> dict[str, AgentToolDefinition]:
    registry: dict[str, AgentToolDefinition] = {}
    for definition in AGENT_TOOL_DEFINITIONS:
        if definition.name in registry:
            raise RuntimeError(f"duplicate agent tool definition: {definition.name}")
        registry[definition.name] = definition
    return registry


AGENT_TOOL_REGISTRY: dict[str, AgentToolDefinition] = _registry_by_name()
RECOMMENDED_FLOW: tuple[str, ...] = ("healthcheck", "scan_opportunities", "get_close_advice")


def write_tools_enabled_from_env() -> bool:
    return _write_tools_enabled_from_config()


def tool_names() -> tuple[str, ...]:
    return tuple(definition.name for definition in AGENT_TOOL_DEFINITIONS)


def get_tool_definition(name: str) -> AgentToolDefinition | None:
    return AGENT_TOOL_REGISTRY.get(str(name or "").strip())


def build_agent_spec(*, write_tools_enabled: bool | None = None) -> dict[str, Any]:
    if write_tools_enabled is None:
        write_tools_enabled = write_tools_enabled_from_env()
    return {
        "schema_version": "1.0",
        "name": "options-monitor-local-tools",
        "description": "Local agent-facing tools for options-monitor. Read-first by default; write tools require explicit enablement and confirmation.",
        "launcher": {
            "command": ["./om-agent", "run", "--tool", "<tool-name>", "--input-json", "<json>"],
            "add_account_command": ["./om-agent", "add-account", "--market", "us|hk", "--account-label", "<label>", "--account-type", "futu|external_holdings"],
            "edit_account_command": ["./om-agent", "edit-account", "--market", "us|hk", "--account-label", "<label>"],
            "remove_account_command": ["./om-agent", "remove-account", "--market", "us|hk", "--account-label", "<label>"],
        },
        "config": {
            "output_dir_env": "OM_OUTPUT_DIR",
            "write_tools_env": "OM_AGENT_ENABLE_WRITE_TOOLS",
        },
        "defaults": {
            "write_tools_enabled": bool(write_tools_enabled),
            "remote_hosted": False,
            "auto_trade": False,
        },
        "tools": [definition.to_manifest() for definition in AGENT_TOOL_DEFINITIONS],
        "recommended_flow": list(RECOMMENDED_FLOW),
    }
