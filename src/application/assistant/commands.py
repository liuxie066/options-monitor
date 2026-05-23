from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssistantCommandSpec:
    intent_name: str
    tool_name: str | None
    commands: tuple[str, ...]
    arguments: tuple[str, ...] = ()
    read_only: bool = True
    llm_allowed: bool = True
    examples: tuple[str, ...] = ()
    summary: str = ""


AgentCommandSpec = AssistantCommandSpec


LLM_INTENT_SCHEMA_VERSION = "om-llm-intent-v1"

ACCOUNT_VALUES = ("lx", "sy")
POSITION_STATUS_VALUES = ("open", "all")
LOG_KIND_VALUES = ("all", "tool", "state")

ARGUMENT_JSON_SCHEMA: dict[str, dict[str, Any]] = {
    "account": {"type": ["string", "null"], "enum": [*ACCOUNT_VALUES, None]},
    "status": {"type": ["string", "null"], "enum": [*POSITION_STATUS_VALUES, None]},
    "month": {"type": ["string", "null"]},
    "run_id": {"type": ["string", "null"]},
    "kind": {"type": ["string", "null"], "enum": [*LOG_KIND_VALUES, None]},
    "limit": {"type": ["integer", "null"]},
    "lines": {"type": ["integer", "null"]},
}

COMMAND_SPECS: tuple[AssistantCommandSpec, ...] = (
    AssistantCommandSpec(
        intent_name="help",
        tool_name=None,
        commands=("/help", "/?"),
        examples=("帮助", "/help"),
        summary="show supported inbound commands",
    ),
    AssistantCommandSpec(
        intent_name="runtime_status",
        tool_name="runtime_status",
        commands=("/status",),
        examples=("状态", "/status"),
        summary="show runtime status",
    ),
    AssistantCommandSpec(
        intent_name="healthcheck",
        tool_name="healthcheck",
        commands=("/health", "/doctor"),
        examples=("健康检查", "/health"),
        summary="run read-only health checks",
    ),
    AssistantCommandSpec(
        intent_name="config_validate",
        tool_name="config_validate",
        commands=("/config-check", "/config"),
        examples=("配置检查", "/config-check"),
        summary="validate runtime config",
    ),
    AssistantCommandSpec(
        intent_name="option_positions_open",
        tool_name="option_positions_read",
        commands=("/positions",),
        arguments=("account", "status"),
        examples=("持仓", "持仓 sy", "/positions [lx|sy|all]"),
        summary="list option positions",
    ),
    AssistantCommandSpec(
        intent_name="monthly_income_report",
        tool_name="monthly_income_report",
        commands=("/income",),
        arguments=("account", "month"),
        examples=("收益", "收益 sy", "收益 sy 2026-05", "/income [lx|sy] [YYYY-MM|本月|上月]"),
        summary="show monthly income report",
    ),
    AssistantCommandSpec(
        intent_name="runtime_runs",
        tool_name="runtime_runs",
        commands=("/runs",),
        arguments=("limit",),
        examples=("最近运行", "/runs [limit]"),
        summary="list recent runtime runs",
    ),
    AssistantCommandSpec(
        intent_name="runtime_logs",
        tool_name="runtime_logs",
        commands=("/logs",),
        arguments=("run_id", "kind", "lines"),
        examples=("日志 <run_id>", "/logs <run_id>"),
        summary="show runtime logs for a run",
    ),
    AssistantCommandSpec(
        intent_name="symbol_list",
        tool_name="inbound.symbols",
        commands=("/symbols",),
        examples=("查看监控标的", "/symbols"),
        summary="list monitored symbols",
    ),
    AssistantCommandSpec(
        intent_name="pending_operations",
        tool_name="inbound.pending",
        commands=("/pending",),
        examples=("待确认", "/pending"),
        summary="list pending preview operations",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_confirm",
        tool_name="inbound.manual_trade",
        commands=("/confirm",),
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        examples=("/confirm trade|symbol|upgrade [operation_id]",),
        summary="confirm a pending manual trade preview",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_cancel",
        tool_name="inbound.manual_trade",
        commands=("/cancel",),
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        examples=("/cancel trade|symbol|upgrade [operation_id]",),
        summary="cancel a pending manual trade preview",
    ),
    AssistantCommandSpec(
        intent_name="symbol_confirm",
        tool_name="inbound.symbols",
        commands=("/confirm",),
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        examples=("/confirm trade|symbol|upgrade [operation_id]",),
        summary="confirm a pending symbol preview",
    ),
    AssistantCommandSpec(
        intent_name="symbol_cancel",
        tool_name="inbound.symbols",
        commands=("/cancel",),
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        examples=("/cancel trade|symbol|upgrade [operation_id]",),
        summary="cancel a pending symbol preview",
    ),
    AssistantCommandSpec(
        intent_name="upgrade_confirm",
        tool_name="inbound.upgrade",
        commands=("/confirm",),
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        examples=("/confirm trade|symbol|upgrade [operation_id]",),
        summary="confirm a pending upgrade preview",
    ),
    AssistantCommandSpec(
        intent_name="upgrade_cancel",
        tool_name="inbound.upgrade",
        commands=("/cancel",),
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        examples=("/cancel trade|symbol|upgrade [operation_id]",),
        summary="cancel a pending upgrade preview",
    ),
)


def command_specs() -> tuple[AssistantCommandSpec, ...]:
    return COMMAND_SPECS


def command_catalog_payload() -> dict[str, Any]:
    specs = [
        {
            "intent_name": spec.intent_name,
            "tool_name": spec.tool_name,
            "commands": list(spec.commands),
            "arguments": list(spec.arguments),
            "read_only": bool(spec.read_only),
            "llm_allowed": bool(spec.llm_allowed),
            "examples": list(spec.examples),
            "summary": spec.summary,
        }
        for spec in COMMAND_SPECS
    ]
    return {
        "summary": {
            "command_count": len(specs),
            "read_only_count": sum(1 for item in specs if item["read_only"]),
            "llm_allowed_count": sum(1 for item in specs if item["llm_allowed"]),
            "write_command_count": sum(1 for item in specs if not item["read_only"]),
        },
        "schema_version": LLM_INTENT_SCHEMA_VERSION,
        "commands": specs,
        "help_text": command_help_text(),
    }


def spec_by_intent() -> dict[str, AssistantCommandSpec]:
    return {spec.intent_name: spec for spec in COMMAND_SPECS}


def commands_by_intent() -> dict[str, tuple[str, ...]]:
    return {spec.intent_name: spec.commands for spec in COMMAND_SPECS}


def llm_allowed_specs() -> tuple[AssistantCommandSpec, ...]:
    return tuple(spec for spec in COMMAND_SPECS if spec.llm_allowed)


def llm_allowed_arguments() -> dict[str, frozenset[str]]:
    return {
        spec.intent_name: frozenset(spec.arguments)
        for spec in llm_allowed_specs()
    }


def llm_intent_names() -> list[str]:
    return sorted(spec.intent_name for spec in llm_allowed_specs())


def llm_argument_schema_properties() -> dict[str, dict[str, Any]]:
    names = sorted({arg for spec in llm_allowed_specs() for arg in spec.arguments})
    return {name: dict(ARGUMENT_JSON_SCHEMA[name]) for name in names}


def llm_argument_schema_required_keys() -> list[str]:
    return sorted({arg for spec in llm_allowed_specs() for arg in spec.arguments})


def command_help_text() -> str:
    return "\n".join(
        [
            "我可以帮你处理这些事：",
            "",
            "只读查询",
            "- 状态：状态、/status",
            "- 健康检查：健康检查、自检、/health",
            "- 配置检查：配置检查、/config-check",
            "- 持仓：持仓、持仓 sy、/positions [lx|sy|all]",
            "- 收益：收益、收益 sy、收益 sy 2026-05、/income [lx|sy] [YYYY-MM|本月|上月]",
            "- 运行记录：最近运行、/runs [limit]",
            "- 日志：日志 <run_id>、/logs <run_id>",
            "- 监控标的：查看监控标的、/symbols",
            "- 待确认：待确认、/pending",
            "",
            "写操作",
            "- 记录交易：记录开仓、记录平仓",
            "- 管理监控标的：增加/修改/删除监控标的",
            "- 升级：立即升级",
            "",
            "安全规则",
            "- 写操作只会先返回预览，不会直接执行。",
            "- 同一对话只有一条待确认时，可回复：确认记录、确认监控、确认升级。",
            "- 指定确认：/confirm trade|symbol|upgrade [operation_id]",
            "- 指定取消：/cancel trade|symbol|upgrade [operation_id]",
            "",
            "Command：/help、/status、/health、/config-check、/positions、/income、/runs、/logs、/symbols、/pending。",
        ]
    )


def _unique(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        out.append(value)
        seen.add(value)
    return out
