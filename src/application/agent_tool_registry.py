from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from src.application.agent_tool_config import write_tools_enabled as _write_tools_enabled_from_config
from src.application.agent_tools.base import AgentTool
from src.application.agent_tools.candidate import CANDIDATE_FILTER_EXPLAIN_TOOL, CANDIDATE_RANK_EXPLAIN_TOOL
from src.application.agent_tools.close_advice import CLOSE_ADVICE_READ_TOOL
from src.application.agent_tools.config import CONFIG_VALIDATE_TOOL, SCHEDULER_STATUS_TOOL
from src.application.agent_tools.diagnostics import (
    HEALTHCHECK_TOOL,
    OPENCLAW_READINESS_TOOL,
    OPERATION_TIMELINE_TOOL,
    RUNTIME_STATUS_TOOL,
)
from src.application.agent_tools.notifications import PREVIEW_NOTIFICATION_TOOL
from src.application.agent_tools.positions import MONTHLY_INCOME_REPORT_TOOL, OPTION_POSITIONS_READ_TOOL
from src.application.agent_tools.runtime import RUNTIME_LOGS_TOOL, RUNTIME_RUNS_TOOL, VERSION_CHECK_TOOL

WriteRequestPredicate = Callable[[dict[str, Any]], bool]


def _version_update_write_requested(payload: dict[str, Any]) -> bool:
    return bool(payload.get("apply", False))


def _manage_symbols_write_requested(payload: dict[str, Any]) -> bool:
    action = str(payload.get("action") or "list").strip().lower()
    return action != "list" and not bool(payload.get("dry_run", False))


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
    write_request_predicate: WriteRequestPredicate | None = field(default=None, repr=False, compare=False)

    def resolved_risk_level(self) -> str:
        return self.risk_level or ("local_write" if self.side_effects else "read_only")

    def is_pure_read(self) -> bool:
        return (
            bool(self.read_only)
            and self.resolved_risk_level() == "read_only"
            and not self.side_effects
            and not self.requires_confirm
        )

    def is_write_requested(self, payload: dict[str, Any]) -> bool:
        if self.read_only:
            return False
        if self.write_request_predicate is not None:
            return bool(self.write_request_predicate(payload))
        if bool(payload.get("dry_run", False)):
            return False
        return bool(self.side_effects or self.requires_confirm or self.resolved_risk_level() != "read_only")

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
            "risk_level": self.resolved_risk_level(),
            "requires_confirm": bool(self.requires_confirm),
            "requires_env": list(self.requires_env),
            "safe_default_input": dict(self.safe_default_input),
            "examples": deepcopy(list(self.examples)),
        }


AgentToolEntry = AgentToolDefinition | AgentTool


AGENT_TOOL_DEFINITIONS: tuple[AgentToolEntry, ...] = (
    HEALTHCHECK_TOOL,
    VERSION_CHECK_TOOL,
    AgentToolDefinition(
        name="version_update",
        read_only=False,
        description="Preview or update local VERSION. Does not create git tags, commit, push, or run release workflows.",
        requires=("local_repo",),
        capabilities=("version_update", "local_write", "release_metadata"),
        side_effects=("writes_VERSION",),
        input_schema={
            "target_version": "optional explicit semver target such as 1.2.3",
            "bump": "optional major|minor|patch; defaults to patch when no version is provided",
            "apply": "optional bool; default false previews only",
            "confirm": "required true when apply=true",
            "allow_downgrade": "optional bool; default false rejects lower target versions",
        },
        risk_level="local_write",
        requires_confirm=True,
        safe_default_input={"bump": "patch", "apply": False},
        write_request_predicate=_version_update_write_requested,
        examples=(
            {"input": {"bump": "patch", "apply": False}},
            {"input": {"target_version": "1.2.3", "apply": True, "confirm": True}},
        ),
    ),
    CONFIG_VALIDATE_TOOL,
    SCHEDULER_STATUS_TOOL,
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
        safe_default_input={"top_n": 5},
        examples=({"input": {"config_key": "us", "top_n": 5}},),
    ),
    CANDIDATE_RANK_EXPLAIN_TOOL,
    CANDIDATE_FILTER_EXPLAIN_TOOL,
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
        safe_default_input={},
        examples=(
            {"input": {"config_key": "us", "account": "lx"}},
            {"input": {"config_key": "us", "account": "sy"}},
        ),
    ),
    MONTHLY_INCOME_REPORT_TOOL,
    OPTION_POSITIONS_READ_TOOL,
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
        safe_default_input={},
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
            "output_dir": "optional output root; defaults to output_shared/agent_tools",
            "ttl_sec": "optional int",
            "timeout_sec": "optional int",
        },
        risk_level="local_write",
        safe_default_input={},
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
            "output_dir": "optional output root; defaults to output_shared/agent_tools",
            "context_path": "optional explicit option_positions_context.json path",
            "required_data_root": "optional explicit required_data root",
        },
        risk_level="local_write",
        safe_default_input={},
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
            "output_dir": "optional output root; defaults to output_shared/agent_tools",
            "ttl_sec": "optional int",
            "timeout_sec": "optional int",
        },
        risk_level="local_write",
        safe_default_input={},
        examples=({"input": {"config_key": "us"}},),
    ),
    CLOSE_ADVICE_READ_TOOL,
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
        safe_default_input={"action": "list"},
        write_request_predicate=_manage_symbols_write_requested,
        examples=(
            {"input": {"config_key": "us", "action": "list"}},
            {"input": {"config_key": "us", "action": "add", "symbol": "NVDA", "dry_run": True}},
        ),
    ),
    PREVIEW_NOTIFICATION_TOOL,
    RUNTIME_STATUS_TOOL,
    RUNTIME_RUNS_TOOL,
    RUNTIME_LOGS_TOOL,
    OPERATION_TIMELINE_TOOL,
    OPENCLAW_READINESS_TOOL,
)


def _registry_by_name() -> dict[str, AgentToolEntry]:
    registry: dict[str, AgentToolEntry] = {}
    for definition in AGENT_TOOL_DEFINITIONS:
        if isinstance(definition, AgentTool) and not definition.enabled:
            continue
        if definition.name in registry:
            raise RuntimeError(f"duplicate agent tool definition: {definition.name}")
        registry[definition.name] = definition
    return registry


AGENT_TOOL_REGISTRY: dict[str, AgentToolEntry] = _registry_by_name()
RECOMMENDED_FLOW: tuple[str, ...] = ("healthcheck", "scan_opportunities", "get_close_advice")


def write_tools_enabled_from_env() -> bool:
    return _write_tools_enabled_from_config()


def tool_names() -> tuple[str, ...]:
    return tuple(
        definition.name
        for definition in AGENT_TOOL_DEFINITIONS
        if not isinstance(definition, AgentTool) or definition.enabled
    )


def get_tool_definition(name: str) -> AgentToolEntry | None:
    return AGENT_TOOL_REGISTRY.get(str(name or "").strip())


def pure_read_tool_names() -> frozenset[str]:
    return frozenset(
        definition.name
        for definition in AGENT_TOOL_DEFINITIONS
        if (not isinstance(definition, AgentTool) or definition.enabled) and definition.is_pure_read()
    )


def tool_write_requested(definition: AgentToolEntry, payload: dict[str, Any]) -> bool:
    return definition.is_write_requested(payload)


def build_agent_spec(*, write_tools_enabled: bool | None = None) -> dict[str, Any]:
    if write_tools_enabled is None:
        write_tools_enabled = write_tools_enabled_from_env()
    return {
        "schema_version": "1.0",
        "name": "options-monitor-local-tools",
        "description": "Local Ops Copilot tools for options-monitor. Read-first by default; write tools require explicit enablement and confirmation.",
        "launcher": {
            "command": ["./om-agent", "run", "--tool", "<tool-name>", "--input-json", "<json>"],
            "add_account_command": ["./om-agent", "add-account", "--market", "us|hk", "--account-label", "<label>", "--account-type", "futu|external_holdings", "--dry-run"],
            "edit_account_command": ["./om-agent", "edit-account", "--market", "us|hk", "--account-label", "<label>", "--dry-run"],
            "remove_account_command": ["./om-agent", "remove-account", "--market", "us|hk", "--account-label", "<label>", "--dry-run"],
        },
        "config": {
            "output_dir_env": "OM_OUTPUT_DIR",
            "write_tools_env": "OM_AGENT_ENABLE_WRITE_TOOLS",
            "openclaw_profile_names": ["openclaw.profile.json", ".openclaw-profile.json"],
        },
        "defaults": {
            "write_tools_enabled": bool(write_tools_enabled),
            "remote_hosted": False,
            "auto_trade": False,
        },
        "tools": [
            definition.to_manifest()
            for definition in AGENT_TOOL_DEFINITIONS
            if not isinstance(definition, AgentTool) or definition.enabled
        ],
        "recommended_flow": list(RECOMMENDED_FLOW),
    }
