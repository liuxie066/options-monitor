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
    llm_visible: bool = True
    risk_level: str | None = None
    examples: tuple[str, ...] = ()
    summary: str = ""


AssistantCapabilitySpec = AssistantCommandSpec
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
        risk_level="confirm_write",
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
        risk_level="confirm_write",
        examples=("/cancel trade|symbol|upgrade [operation_id]",),
        summary="cancel a pending manual trade preview",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_open",
        tool_name="inbound.manual_trade",
        commands=(),
        arguments=("raw_text",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("记录开仓", "record open"),
        summary="preview a manual opening trade record",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_close",
        tool_name="inbound.manual_trade",
        commands=(),
        arguments=("raw_text",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("记录平仓", "record close"),
        summary="preview a manual closing trade record",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_update",
        tool_name="inbound.manual_trade",
        commands=(),
        arguments=("operation_id", "operation_resolution", "updates"),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("权利金改成 1.23", "合约数改成 2"),
        summary="update a pending manual trade preview",
    ),
    AssistantCommandSpec(
        intent_name="symbol_confirm",
        tool_name="inbound.symbols",
        commands=("/confirm",),
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
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
        risk_level="confirm_write",
        examples=("/cancel trade|symbol|upgrade [operation_id]",),
        summary="cancel a pending symbol preview",
    ),
    AssistantCommandSpec(
        intent_name="symbol_add",
        tool_name="inbound.symbols",
        commands=(),
        arguments=("symbol", "sell_put_enabled", "sell_call_enabled"),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("增加监控标的 700 put",),
        summary="preview adding a monitored symbol",
    ),
    AssistantCommandSpec(
        intent_name="symbol_edit",
        tool_name="inbound.symbols",
        commands=(),
        arguments=("symbol", "set"),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("修改监控标的 HK.00700 sell_put.max_strike=480",),
        summary="preview editing a monitored symbol",
    ),
    AssistantCommandSpec(
        intent_name="symbol_remove",
        tool_name="inbound.symbols",
        commands=(),
        arguments=("symbol",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("删除监控标的 腾讯",),
        summary="preview removing a monitored symbol",
    ),
    AssistantCommandSpec(
        intent_name="upgrade_confirm",
        tool_name="inbound.upgrade",
        commands=("/confirm",),
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
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
        risk_level="confirm_write",
        examples=("/cancel trade|symbol|upgrade [operation_id]",),
        summary="cancel a pending upgrade preview",
    ),
    AssistantCommandSpec(
        intent_name="upgrade_now",
        tool_name="inbound.upgrade",
        commands=(),
        arguments=("target_version",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_admin",
        examples=("立即升级", "立即升级到 v1.2.111"),
        summary="preview a software upgrade operation",
    ),
)


def command_specs() -> tuple[AssistantCommandSpec, ...]:
    return COMMAND_SPECS


def capability_specs() -> tuple[AssistantCapabilitySpec, ...]:
    return COMMAND_SPECS


def command_catalog_payload() -> dict[str, Any]:
    specs = [_spec_payload(spec) for spec in COMMAND_SPECS]
    return {
        "summary": {
            "command_count": len(specs),
            "capability_count": len(specs),
            "slash_command_count": len({command for spec in COMMAND_SPECS for command in spec.commands}),
            "read_only_count": sum(1 for item in specs if item["read_only"]),
            "llm_allowed_count": sum(1 for item in specs if item["llm_allowed"]),
            "llm_executable_count": sum(1 for item in specs if item["llm_executable"]),
            "write_command_count": sum(1 for item in specs if not item["read_only"]),
            "write_capability_count": sum(1 for item in specs if not item["read_only"]),
        },
        "schema_version": LLM_INTENT_SCHEMA_VERSION,
        "commands": specs,
        "capabilities": specs,
        "help_text": command_help_text(),
    }


def capability_catalog_payload() -> dict[str, Any]:
    return command_catalog_payload()


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


def llm_capability_manifest() -> dict[str, Any]:
    capabilities = [
        _spec_payload(spec)
        for spec in COMMAND_SPECS
        if spec.llm_visible
    ]
    return {
        "schema_version": LLM_INTENT_SCHEMA_VERSION,
        "intent_field_semantics": "The JSON `intent` field is the OM capability_id.",
        "routing_rule": "Choose only capabilities where llm_executable is true. For write, confirm, admin, or unknown requests, return low confidence.",
        "llm_executable_intents": llm_intent_names(),
        "capabilities": capabilities,
    }


def llm_capability_prompt() -> str:
    manifest = llm_capability_manifest()
    lines = [
        "Available OM capabilities:",
        "The JSON `intent` field must be one executable capability_id from this manifest.",
        "Capabilities with llm_executable=false are known project abilities but must not be routed by LLM.",
    ]
    for item in manifest["capabilities"]:
        executable = "true" if item["llm_executable"] else "false"
        commands = ", ".join(item["commands"]) if item["commands"] else "-"
        examples = " | ".join(item["examples"]) if item["examples"] else "-"
        args = ", ".join(item["arguments"]) if item["arguments"] else "-"
        lines.append(
            f"- {item['capability_id']}: {item['summary']}; risk={item['risk_level']}; "
            f"llm_executable={executable}; commands={commands}; args={args}; examples={examples}"
        )
    return "\n".join(lines)


def llm_argument_schema_properties() -> dict[str, dict[str, Any]]:
    names = sorted({arg for spec in llm_allowed_specs() for arg in spec.arguments})
    return {name: dict(ARGUMENT_JSON_SCHEMA[name]) for name in names}


def llm_argument_schema_required_keys() -> list[str]:
    return sorted({arg for spec in llm_allowed_specs() for arg in spec.arguments})


def _spec_payload(spec: AssistantCommandSpec) -> dict[str, Any]:
    return {
        "capability_id": spec.intent_name,
        "intent_name": spec.intent_name,
        "tool_name": spec.tool_name,
        "commands": list(spec.commands),
        "arguments": list(spec.arguments),
        "read_only": bool(spec.read_only),
        "llm_allowed": bool(spec.llm_allowed),
        "llm_visible": bool(spec.llm_visible),
        "llm_executable": bool(spec.read_only and spec.llm_allowed),
        "risk_level": _risk_level(spec),
        "examples": list(spec.examples),
        "summary": spec.summary,
    }


def _risk_level(spec: AssistantCommandSpec) -> str:
    if spec.risk_level:
        return spec.risk_level
    return "read_only" if spec.read_only else "write"


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
