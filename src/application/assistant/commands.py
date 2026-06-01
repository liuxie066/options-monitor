from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssistantCommandSpec:
    intent_name: str
    tool_name: str | None
    commands: tuple[str, ...]
    display_name: str
    arguments: tuple[str, ...] = ()
    read_only: bool = True
    llm_allowed: bool = True
    llm_visible: bool = True
    supported: bool = True
    risk_level: str | None = None
    examples: tuple[str, ...] = ()
    summary: str = ""
    operation_action: str | None = None
    operation_target: str | None = None
    operation_target_aliases: tuple[str, ...] = ()


AssistantCapabilitySpec = AssistantCommandSpec
AgentCommandSpec = AssistantCommandSpec


LLM_INTENT_SCHEMA_VERSION = "om-llm-intent-v1"

ACCOUNT_VALUES = ("lx", "sy")
POSITION_STATUS_VALUES = ("open", "close", "all")
LOG_KIND_VALUES = ("all", "tool", "state")

ARGUMENT_JSON_SCHEMA: dict[str, dict[str, Any]] = {
    "account": {"type": ["string", "null"], "enum": [*ACCOUNT_VALUES, None]},
    "status": {"type": ["string", "null"], "enum": [*POSITION_STATUS_VALUES, None]},
    "symbol": {"type": ["string", "null"]},
    "option_type": {"type": ["string", "null"], "enum": ["put", "call", None]},
    "side": {"type": ["string", "null"], "enum": ["short", "long", None]},
    "strike": {"type": ["number", "null"]},
    "expiration": {
        "type": ["object", "null"],
        "additionalProperties": False,
        "properties": {
            "exact": {"type": ["string", "null"]},
            "month": {"type": ["string", "null"]},
            "before": {"type": ["string", "null"]},
            "after": {"type": ["string", "null"]},
            "within_days": {"type": ["integer", "null"]},
        },
    },
    "month": {"type": ["string", "null"]},
    "run_id": {"type": ["string", "null"]},
    "kind": {"type": ["string", "null"], "enum": [*LOG_KIND_VALUES, None]},
    "limit": {"type": ["integer", "null"]},
    "lines": {"type": ["integer", "null"]},
    "model_profile": {"type": ["string", "null"]},
    "set": {
        "type": ["object", "null"],
        "additionalProperties": {"type": ["string", "number", "integer", "boolean", "null"]},
    },
    "ensure_use": {
        "type": ["array", "null"],
        "items": {"type": "string"},
        "maxItems": 8,
    },
}

COMMAND_SPECS: tuple[AssistantCommandSpec, ...] = (
    AssistantCommandSpec(
        intent_name="help",
        tool_name=None,
        commands=("/help", "/?"),
        display_name="帮助",
        examples=("帮助", "/help"),
        summary="show supported inbound commands",
    ),
    AssistantCommandSpec(
        intent_name="runtime_status",
        tool_name="runtime_status",
        commands=("/status",),
        display_name="状态",
        examples=("状态", "/status"),
        summary="show runtime status",
    ),
    AssistantCommandSpec(
        intent_name="healthcheck",
        tool_name="healthcheck",
        commands=("/health", "/doctor"),
        display_name="健康检查",
        examples=("健康检查", "/health"),
        summary="run read-only health checks",
    ),
    AssistantCommandSpec(
        intent_name="config_validate",
        tool_name="config_validate",
        commands=("/config-check", "/config"),
        display_name="配置检查",
        examples=("配置检查", "/config-check"),
        summary="validate runtime config",
    ),
    AssistantCommandSpec(
        intent_name="position_query",
        tool_name="option_positions_read",
        commands=("/positions",),
        display_name="持仓",
        arguments=("account", "status", "symbol", "option_type", "side", "strike", "expiration", "limit"),
        examples=("持仓", "持仓 [账户]", "持仓 [到期月份/到期日/标的/类型/方向]", "/positions [lx|sy|all]"),
        summary="list option positions",
    ),
    AssistantCommandSpec(
        intent_name="position_exit_analysis",
        tool_name="close_advice_read",
        commands=(),
        display_name="平仓/止盈分析",
        arguments=("account", "symbol", "option_type", "side", "strike", "expiration", "limit"),
        read_only=True,
        llm_allowed=True,
        supported=True,
        risk_level="read_only",
        examples=("分析 long call 是不是应该平仓", "泡泡玛特 long call 的持仓应该止盈吗"),
        summary="analyze matching option positions using the latest generated close-advice report",
    ),
    AssistantCommandSpec(
        intent_name="monthly_income_report",
        tool_name="monthly_income_report",
        commands=("/income",),
        display_name="收益",
        arguments=("account", "month"),
        examples=("收益", "收益 [账户]", "收益 [账户] [YYYY-MM|6月|本月|上月]", "/income [lx|sy] [YYYY-MM|6月|本月|上月]"),
        summary="show monthly income report",
    ),
    AssistantCommandSpec(
        intent_name="runtime_runs",
        tool_name="runtime_runs",
        commands=("/runs",),
        display_name="运行记录",
        arguments=("limit",),
        examples=("最近运行", "/runs [limit]"),
        summary="list recent runtime runs",
    ),
    AssistantCommandSpec(
        intent_name="runtime_logs",
        tool_name="runtime_logs",
        commands=("/logs",),
        display_name="日志",
        arguments=("run_id", "kind", "lines"),
        examples=("日志 <run_id>", "/logs <run_id>"),
        summary="show runtime logs for a run",
    ),
    AssistantCommandSpec(
        intent_name="symbol_list",
        tool_name="inbound.symbols",
        commands=("/symbols",),
        display_name="监控标的",
        examples=("查看监控标的", "/symbols"),
        summary="list monitored symbols",
    ),
    AssistantCommandSpec(
        intent_name="pending_operations",
        tool_name="inbound.pending",
        commands=("/pending",),
        display_name="待确认",
        examples=("待确认", "/pending"),
        summary="list pending preview operations",
    ),
    AssistantCommandSpec(
        intent_name="model_list",
        tool_name="inbound.model",
        commands=("/model",),
        display_name="模型",
        llm_allowed=False,
        examples=("/model", "/model list"),
        summary="list configured assistant model profiles",
    ),
    AssistantCommandSpec(
        intent_name="model_use",
        tool_name="inbound.model",
        commands=("/model",),
        display_name="切换模型",
        arguments=("model_profile",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("/model use <name>",),
        summary="preview switching assistant.active_model",
        operation_action="preview",
        operation_target="model",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_confirm",
        tool_name="inbound.manual_trade",
        commands=("/confirm",),
        display_name="确认交易记录",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
        examples=("确认记录", "/confirm trade|symbol|upgrade|model [operation_id]"),
        summary="confirm a pending manual trade preview",
        operation_action="confirm",
        operation_target="trade",
        operation_target_aliases=("trade", "record", "records", "manual", "记录", "交易"),
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_cancel",
        tool_name="inbound.manual_trade",
        commands=("/cancel",),
        display_name="取消交易记录",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
        examples=("取消记录", "/cancel trade|symbol|upgrade|model [operation_id]"),
        summary="cancel a pending manual trade preview",
        operation_action="cancel",
        operation_target="trade",
        operation_target_aliases=("trade", "record", "records", "manual", "记录", "交易"),
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_open",
        tool_name="inbound.manual_trade",
        commands=("/record-open",),
        display_name="记录开仓",
        arguments=("raw_text",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=(
            "记录开仓",
            "record open",
            "/record-open [账户] <标的> <short|long> <put|call> strike <行权价> exp <YYYY-MM-DD> <张数>张 premium <权利金> multiplier <乘数>",
        ),
        summary="preview a manual opening trade record",
        operation_action="preview",
        operation_target="trade",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_close",
        tool_name="inbound.manual_trade",
        commands=("/record-close",),
        display_name="记录平仓",
        arguments=("raw_text",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=(
            "记录平仓",
            "record close",
            "/record-close record_id=<record_id> <张数>张 close <平仓价>",
        ),
        summary="preview a manual closing trade record",
        operation_action="preview",
        operation_target="trade",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_update",
        tool_name="inbound.manual_trade",
        commands=(),
        display_name="修改待确认交易",
        arguments=("operation_id", "operation_resolution", "updates"),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("<字段>改成<值> [operation_id]", "<field>=<value> [operation_id]"),
        summary="update a pending manual trade preview",
        operation_action="preview",
        operation_target="trade",
    ),
    AssistantCommandSpec(
        intent_name="symbol_confirm",
        tool_name="inbound.symbols",
        commands=("/confirm",),
        display_name="确认监控变更",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
        examples=("确认监控", "/confirm trade|symbol|upgrade|model [operation_id]"),
        summary="confirm a pending symbol preview",
        operation_action="confirm",
        operation_target="symbol",
        operation_target_aliases=("symbol", "symbols", "monitor", "监控"),
    ),
    AssistantCommandSpec(
        intent_name="symbol_cancel",
        tool_name="inbound.symbols",
        commands=("/cancel",),
        display_name="取消监控变更",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
        examples=("取消监控", "/cancel trade|symbol|upgrade|model [operation_id]"),
        summary="cancel a pending symbol preview",
        operation_action="cancel",
        operation_target="symbol",
        operation_target_aliases=("symbol", "symbols", "monitor", "监控"),
    ),
    AssistantCommandSpec(
        intent_name="symbol_add",
        tool_name="inbound.symbols",
        commands=(),
        display_name="增加监控标的",
        arguments=("symbol", "sell_put_enabled", "sell_call_enabled"),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("增加监控标的 <symbol> [put|call]",),
        summary="preview adding a monitored symbol",
        operation_action="preview",
        operation_target="symbol",
    ),
    AssistantCommandSpec(
        intent_name="symbol_edit",
        tool_name="inbound.symbols",
        commands=(),
        display_name="修改监控标的",
        arguments=("symbol", "set", "ensure_use"),
        read_only=False,
        llm_allowed=True,
        risk_level="preview_write",
        examples=("设置 09898 covered call min strike 85", "修改监控标的 <symbol> <field>=<value>"),
        summary="preview editing covered-call or sell-put monitored-symbol settings",
        operation_action="preview",
        operation_target="symbol",
    ),
    AssistantCommandSpec(
        intent_name="symbol_remove",
        tool_name="inbound.symbols",
        commands=(),
        display_name="删除监控标的",
        arguments=("symbol",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_write",
        examples=("删除监控标的 <symbol>",),
        summary="preview removing a monitored symbol",
        operation_action="preview",
        operation_target="symbol",
    ),
    AssistantCommandSpec(
        intent_name="upgrade_confirm",
        tool_name="inbound.upgrade",
        commands=("/confirm",),
        display_name="确认升级",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
        examples=("确认升级", "/confirm trade|symbol|upgrade|model [operation_id]"),
        summary="confirm a pending upgrade preview",
        operation_action="confirm",
        operation_target="upgrade",
        operation_target_aliases=("upgrade", "升级"),
    ),
    AssistantCommandSpec(
        intent_name="upgrade_cancel",
        tool_name="inbound.upgrade",
        commands=("/cancel",),
        display_name="取消升级",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
        examples=("取消升级", "/cancel trade|symbol|upgrade|model [operation_id]"),
        summary="cancel a pending upgrade preview",
        operation_action="cancel",
        operation_target="upgrade",
        operation_target_aliases=("upgrade", "升级"),
    ),
    AssistantCommandSpec(
        intent_name="model_confirm",
        tool_name="inbound.model",
        commands=("/confirm",),
        display_name="确认模型切换",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
        examples=("确认模型", "/confirm trade|symbol|upgrade|model [operation_id]"),
        summary="confirm a pending assistant model switch",
        operation_action="confirm",
        operation_target="model",
        operation_target_aliases=("model", "models", "模型"),
    ),
    AssistantCommandSpec(
        intent_name="model_cancel",
        tool_name="inbound.model",
        commands=("/cancel",),
        display_name="取消模型切换",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        llm_allowed=False,
        risk_level="confirm_write",
        examples=("取消模型", "/cancel trade|symbol|upgrade|model [operation_id]"),
        summary="cancel a pending assistant model switch",
        operation_action="cancel",
        operation_target="model",
        operation_target_aliases=("model", "models", "模型"),
    ),
    AssistantCommandSpec(
        intent_name="upgrade_now",
        tool_name="inbound.upgrade",
        commands=(),
        display_name="立即升级",
        arguments=("target_version",),
        read_only=False,
        llm_allowed=False,
        risk_level="preview_admin",
        examples=("立即升级", "立即升级到 v<version>"),
        summary="preview a software upgrade operation",
        operation_action="preview",
        operation_target="upgrade",
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
            "llm_allowed_count": sum(1 for item in specs if item["llm_executable"]),
            "llm_executable_count": sum(1 for item in specs if item["llm_executable"]),
            "llm_recognizable_count": sum(1 for item in specs if item["llm_recognizable"]),
            "write_command_count": sum(1 for item in specs if not item["read_only"]),
            "write_capability_count": sum(1 for item in specs if not item["read_only"]),
        },
        "schema_version": LLM_INTENT_SCHEMA_VERSION,
        "commands": specs,
        "capabilities": specs,
        "help_text": command_help_text(),
    }


def capability_catalog_payload() -> dict[str, Any]:
    payload = command_catalog_payload()
    payload["capability_text"] = capability_catalog_text(payload)
    return payload


def capability_catalog_text(payload: dict[str, Any] | None = None) -> str:
    catalog = payload if payload is not None else command_catalog_payload()
    capabilities = list(catalog.get("capabilities") or [])
    executable = [item for item in capabilities if item.get("llm_executable")]
    recognizable = [item for item in capabilities if item.get("llm_recognizable") and not item.get("llm_executable")]
    non_executable = [item for item in capabilities if not item.get("llm_recognizable")]

    lines = [
        "Assistant capabilities",
        "",
        "LLM executable read-only capabilities:",
    ]
    lines.extend(_capability_text_line(item) for item in executable)
    lines.extend([
        "",
        "LLM recognizable but not executable capabilities:",
    ])
    lines.extend(_capability_text_line(item) for item in recognizable)
    lines.extend([
        "",
        "Known capabilities not recognizable by LLM:",
    ])
    lines.extend(_capability_text_line(item) for item in non_executable)
    lines.extend([
        "",
        (
            "Rule: LLM may identify llm_recognizable=true capabilities. The deterministic "
            "reasoning layer decides whether a recognized capability is executable."
        ),
    ])
    return "\n".join(lines)


def spec_by_intent() -> dict[str, AssistantCommandSpec]:
    return {spec.intent_name: spec for spec in COMMAND_SPECS}


def commands_by_intent() -> dict[str, tuple[str, ...]]:
    return {spec.intent_name: spec.commands for spec in COMMAND_SPECS}


def operation_specs(*, action: str | None = None, target: str | None = None) -> tuple[AssistantCommandSpec, ...]:
    return tuple(
        spec
        for spec in COMMAND_SPECS
        if (action is None or spec.operation_action == action)
        and (target is None or spec.operation_target == target)
    )


def operation_target_intents(action: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in operation_specs(action=action):
        aliases = spec.operation_target_aliases or ((spec.operation_target,) if spec.operation_target else ())
        for alias in aliases:
            normalized = str(alias or "").strip().lower()
            if normalized:
                out[normalized] = spec.intent_name
    return out


def llm_executable_specs() -> tuple[AssistantCommandSpec, ...]:
    return tuple(spec for spec in COMMAND_SPECS if _is_llm_executable_spec(spec))


def llm_recognizable_specs() -> tuple[AssistantCommandSpec, ...]:
    return tuple(spec for spec in COMMAND_SPECS if _is_llm_recognizable_spec(spec))


def llm_allowed_specs() -> tuple[AssistantCommandSpec, ...]:
    return llm_recognizable_specs()


def llm_executable_arguments() -> dict[str, frozenset[str]]:
    return {
        spec.intent_name: frozenset(spec.arguments)
        for spec in llm_recognizable_specs()
    }


def llm_allowed_arguments() -> dict[str, frozenset[str]]:
    return llm_executable_arguments()


def llm_executable_intent_names() -> list[str]:
    return sorted(spec.intent_name for spec in llm_executable_specs())


def llm_recognizable_intent_names() -> list[str]:
    return sorted(spec.intent_name for spec in llm_recognizable_specs())


def llm_intent_names() -> list[str]:
    return llm_recognizable_intent_names()


def llm_capability_manifest() -> dict[str, Any]:
    capabilities = [
        _spec_payload(spec)
        for spec in COMMAND_SPECS
        if spec.llm_visible
    ]
    return {
        "schema_version": LLM_INTENT_SCHEMA_VERSION,
        "intent_field_semantics": "The JSON `intent` field is the OM capability_id.",
        "routing_rule": "Choose only capabilities where llm_recognizable is true. The reasoning layer will reject recognized but unsupported capabilities without downgrading them.",
        "llm_executable_intents": llm_executable_intent_names(),
        "llm_recognizable_intents": llm_recognizable_intent_names(),
        "capabilities": capabilities,
    }


def llm_capability_prompt() -> str:
    manifest = llm_capability_manifest()
    lines = [
        "Available OM capabilities:",
        "The JSON `intent` field must be one llm_recognizable capability_id from this manifest.",
        "Capabilities with llm_executable=true are read-only tool calls.",
        "Capabilities with llm_recognizable=true and llm_executable=false may only enter deterministic reasoning; preview-write capabilities can create a pending preview but cannot apply writes.",
        "Capabilities with llm_recognizable=false must not be routed by LLM.",
    ]
    for item in manifest["capabilities"]:
        executable = "true" if item["llm_executable"] else "false"
        recognizable = "true" if item["llm_recognizable"] else "false"
        commands = ", ".join(item["commands"]) if item["commands"] else "-"
        usage = " | ".join(item["examples"]) if item["examples"] else "-"
        args = ", ".join(item["arguments"]) if item["arguments"] else "-"
        lines.append(
            f"- {item['capability_id']} ({item['display_name']}): {item['summary']}; risk={item['risk_level']}; "
            f"llm_recognizable={recognizable}; llm_executable={executable}; commands={commands}; args={args}; usage={usage}"
        )
    return "\n".join(lines)


def llm_argument_schema_properties() -> dict[str, dict[str, Any]]:
    names = sorted({arg for spec in llm_recognizable_specs() for arg in spec.arguments})
    return {name: dict(ARGUMENT_JSON_SCHEMA[name]) for name in names}


def llm_argument_schema_required_keys() -> list[str]:
    return sorted({arg for spec in llm_recognizable_specs() for arg in spec.arguments})


def _spec_payload(spec: AssistantCommandSpec) -> dict[str, Any]:
    return {
        "capability_id": spec.intent_name,
        "intent_name": spec.intent_name,
        "tool_name": spec.tool_name,
        "commands": list(spec.commands),
        "display_name": spec.display_name,
        "arguments": list(spec.arguments),
        "read_only": bool(spec.read_only),
        "llm_allowed": bool(spec.llm_allowed),
        "llm_visible": bool(spec.llm_visible),
        "supported": bool(spec.supported),
        "llm_recognizable": _is_llm_recognizable_spec(spec),
        "llm_executable": _is_llm_executable_spec(spec),
        "risk_level": _risk_level(spec),
        "examples": list(spec.examples),
        "summary": spec.summary,
        "operation_action": spec.operation_action,
        "operation_target": spec.operation_target,
        "operation_target_aliases": list(spec.operation_target_aliases),
        "usage_patterns": list(spec.examples),
    }


def _is_llm_executable_spec(spec: AssistantCommandSpec) -> bool:
    return bool(spec.read_only and spec.llm_allowed and spec.supported and spec.tool_name is not None)


def _is_llm_recognizable_spec(spec: AssistantCommandSpec) -> bool:
    return bool((spec.read_only and spec.llm_allowed) or _is_llm_preview_recognizable_spec(spec))


def _is_llm_preview_recognizable_spec(spec: AssistantCommandSpec) -> bool:
    return bool(
        spec.intent_name == "symbol_edit"
        and spec.llm_allowed
        and not spec.read_only
        and spec.risk_level == "preview_write"
        and spec.operation_action == "preview"
        and spec.operation_target == "symbol"
        and spec.supported
        and spec.tool_name == "inbound.symbols"
    )


def _risk_level(spec: AssistantCommandSpec) -> str:
    if spec.risk_level:
        return spec.risk_level
    return "read_only" if spec.read_only else "write"


def _capability_text_line(item: dict[str, Any]) -> str:
    commands = ", ".join(_unique(item.get("commands") or ())) or "-"
    usage = " | ".join(_unique(item.get("usage_patterns") or item.get("examples") or ())[:3]) or "-"
    arguments = ", ".join(_unique(item.get("arguments") or ())) or "-"
    executable = "true" if item.get("llm_executable") else "false"
    return (
        f"- {item.get('capability_id')} ({item.get('display_name')}): risk={item.get('risk_level')} "
        f"llm_executable={executable} commands={commands} args={arguments} usage={usage}"
    )


def command_help_text() -> str:
    specs = [_spec_payload(spec) for spec in COMMAND_SPECS]
    read_only = [item for item in specs if item["read_only"]]
    preview_writes = [
        item
        for item in specs
        if not item["read_only"] and item["risk_level"] in {"preview_write", "preview_admin"}
    ]
    confirm_shortcuts = _non_slash_examples(
        item for item in specs if not item["read_only"] and item["intent_name"].endswith("_confirm")
    )
    confirm_command = _first_slash_example(
        item for item in specs if not item["read_only"] and item["intent_name"].endswith("_confirm")
    )
    cancel_command = _first_slash_example(
        item for item in specs if not item["read_only"] and item["intent_name"].endswith("_cancel")
    )
    command_line = "、".join(_read_only_slash_commands(read_only))

    lines = [
        "我可以帮你处理这些事：",
        "",
        "只读查询",
    ]
    lines.extend(_help_menu_line(item) for item in read_only)
    lines.extend([
        "",
        "写操作",
    ])
    lines.extend(_help_menu_line(item) for item in preview_writes)
    lines.extend([
        "",
        "安全规则",
        "- 写操作只会先返回预览，不会直接执行。",
    ])
    if confirm_shortcuts:
        lines.append(f"- 同一对话只有一条待确认时，可回复：{'、'.join(confirm_shortcuts)}。")
    if confirm_command:
        lines.append(f"- 指定确认：{confirm_command}")
    if cancel_command:
        lines.append(f"- 指定取消：{cancel_command}")
    if command_line:
        lines.extend(["", f"Command：{command_line}。"])
    return "\n".join(lines)


def _help_menu_line(item: dict[str, Any]) -> str:
    return f"- {item['display_name']}：{_help_examples(item)}"


def _help_examples(item: dict[str, Any]) -> str:
    examples = _unique(item.get("examples") or ())
    commands = [
        command
        for command in _unique(item.get("commands") or ())
        if not _command_is_covered_by_example(command, examples)
    ]
    values = _unique([*examples, *commands])
    return "、".join(values) if values else "-"


def _command_is_covered_by_example(command: str, examples: list[str]) -> bool:
    return any(example == command or example.startswith(f"{command} ") for example in examples)


def _read_only_slash_commands(items: list[dict[str, Any]]) -> list[str]:
    return _unique(command for item in items for command in item.get("commands") or ())


def _non_slash_examples(items: Any) -> list[str]:
    return _unique(
        example
        for item in items
        for example in item.get("examples") or ()
        if not str(example).startswith("/")
    )


def _first_slash_example(items: Any) -> str:
    for item in items:
        for example in item.get("examples") or ():
            value = str(example or "").strip()
            if value.startswith("/"):
                return value
    return ""


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
