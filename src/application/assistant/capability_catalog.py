from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.assistant.tool_bindings import AssistantToolBinding, assistant_tool_bindings


@dataclass(frozen=True)
class AssistantCommandSpec:
    intent_name: str
    tool_name: str | None
    commands: tuple[str, ...]
    display_name: str
    arguments: tuple[str, ...] = ()
    read_only: bool = True
    supported: bool = True
    risk_level: str | None = None
    examples: tuple[str, ...] = ()
    summary: str = ""
    operation_action: str | None = None
    operation_target: str | None = None
    operation_target_aliases: tuple[str, ...] = ()
    kind: str | None = None
    direct_executable: bool | None = None
    requires_pending: bool | None = None
    requires_confirm: bool | None = None
    required_information: tuple[str, ...] = ()


AgentCommandSpec = AssistantCommandSpec


COMMAND_CATALOG_SCHEMA_VERSION = "om-assistant-command-catalog-v1"

def _spec_from_binding(binding: AssistantToolBinding) -> AssistantCommandSpec:
    return AssistantCommandSpec(
        intent_name=binding.intent_name,
        tool_name=binding.tool_name,
        commands=binding.commands,
        display_name=binding.display_name,
        arguments=binding.arguments,
        read_only=binding.read_only,
        supported=binding.supported,
        risk_level=binding.risk_level,
        examples=binding.examples,
        summary=binding.summary,
        kind=binding.kind,
        direct_executable=binding.direct_executable,
        requires_pending=binding.requires_pending,
        requires_confirm=binding.requires_confirm,
    )


def _binding_command_specs() -> tuple[AssistantCommandSpec, ...]:
    return tuple(_spec_from_binding(binding) for binding in assistant_tool_bindings())


COMMAND_SPECS: tuple[AssistantCommandSpec, ...] = (
    *_binding_command_specs(),
    AssistantCommandSpec(
        intent_name="model_use",
        tool_name="inbound.model",
        commands=("/model",),
        display_name="切换模型",
        arguments=("model_profile",),
        read_only=False,
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
        risk_level="confirm_write",
        examples=("/confirm trade [operation_id]", "确认记录"),
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
        risk_level="confirm_write",
        examples=("/cancel trade [operation_id]", "取消记录"),
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
        arguments=("raw_text", "account"),
        read_only=False,
        risk_level="preview_write",
        examples=(
            "记录开仓",
            "record open",
            "/record-open [账户] <标的> <short|long> <put|call> strike <行权价> exp <YYYY-MM-DD> <张数>张 premium <权利金> multiplier <乘数>",
        ),
        summary="preview a manual opening trade record",
        operation_action="preview",
        operation_target="trade",
        required_information=(
            "account",
            "symbol",
            "side",
            "option_type",
            "contracts",
            "strike",
            "expiration_ymd",
            "premium_per_share",
        ),
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_close",
        tool_name="inbound.manual_trade",
        commands=("/record-close",),
        display_name="记录平仓",
        arguments=("raw_text", "account"),
        read_only=False,
        risk_level="preview_write",
        examples=(
            "记录平仓",
            "record close",
            "/record-close record_id=<record_id> <张数>张 close <平仓价>",
        ),
        summary="preview a manual closing trade record",
        operation_action="preview",
        operation_target="trade",
        required_information=(
            "record_id or full contract identity",
            "contracts_to_close",
            "close_price",
        ),
    ),
    AssistantCommandSpec(
        intent_name="manual_assignment",
        tool_name="inbound.manual_trade",
        commands=(),
        display_name="记录被指派",
        arguments=("raw_text", "account"),
        read_only=False,
        risk_level="preview_write",
        examples=(
            "期权被指派通知",
            "已被指派",
        ),
        summary="preview an option assignment lifecycle record",
        operation_action="preview",
        operation_target="trade",
    ),
    AssistantCommandSpec(
        intent_name="manual_expiry",
        tool_name="inbound.manual_trade",
        commands=("/record-expiry",),
        display_name="记录到期失效",
        arguments=("raw_text", "account"),
        read_only=False,
        risk_level="preview_write",
        examples=(
            "期权到期失效通知",
            "已到期失效",
            "/record-expiry <富途期权到期失效通知>",
        ),
        summary="preview an expired-unassigned option lifecycle record",
        operation_action="preview",
        operation_target="trade",
    ),
    AssistantCommandSpec(
        intent_name="manual_trade_update",
        tool_name="inbound.manual_trade",
        commands=("/record-update",),
        display_name="修改待确认交易",
        arguments=("operation_id", "operation_resolution", "updates"),
        read_only=False,
        risk_level="preview_write",
        examples=("/record-update <field>=<value> [operation_id]",),
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
        risk_level="confirm_write",
        examples=("/confirm symbol [operation_id]", "确认监控"),
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
        risk_level="confirm_write",
        examples=("/cancel symbol [operation_id]", "取消监控"),
        summary="cancel a pending symbol preview",
        operation_action="cancel",
        operation_target="symbol",
        operation_target_aliases=("symbol", "symbols", "monitor", "监控"),
    ),
    AssistantCommandSpec(
        intent_name="symbol_add",
        tool_name="inbound.symbols",
        commands=("/symbol", "/symbols"),
        display_name="增加监控标的",
        arguments=("symbol", "sell_put_enabled", "sell_call_enabled"),
        read_only=False,
        risk_level="preview_write",
        examples=("/symbol add <symbol> [put|call]",),
        summary="preview adding a monitored symbol",
        operation_action="preview",
        operation_target="symbol",
    ),
    AssistantCommandSpec(
        intent_name="symbol_edit",
        tool_name="inbound.symbols",
        commands=("/symbol", "/symbols"),
        display_name="修改监控标的",
        arguments=("symbol", "set", "ensure_use"),
        read_only=False,
        risk_level="preview_write",
        examples=("/symbol edit <symbol> <field>=<value>", "/symbol edit 3690.HK combo_yield.enabled=true"),
        summary="preview editing CC, sell-put, or combo-yield monitored-symbol settings",
        operation_action="preview",
        operation_target="symbol",
    ),
    AssistantCommandSpec(
        intent_name="symbol_remove",
        tool_name="inbound.symbols",
        commands=("/symbol", "/symbols"),
        display_name="删除监控标的",
        arguments=("symbol",),
        read_only=False,
        risk_level="preview_write",
        examples=("/symbol remove <symbol>",),
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
        risk_level="confirm_write",
        examples=("/confirm upgrade [operation_id]", "确认升级"),
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
        risk_level="confirm_write",
        examples=("/cancel upgrade [operation_id]", "取消升级"),
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
        risk_level="confirm_write",
        examples=("/confirm model [operation_id]", "确认模型"),
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
        risk_level="confirm_write",
        examples=("/cancel model [operation_id]", "取消模型"),
        summary="cancel a pending assistant model switch",
        operation_action="cancel",
        operation_target="model",
        operation_target_aliases=("model", "models", "模型"),
    ),
    AssistantCommandSpec(
        intent_name="upgrade_now",
        tool_name="inbound.upgrade",
        commands=("/upgrade",),
        display_name="立即升级",
        arguments=("target_version",),
        read_only=False,
        risk_level="preview_admin",
        examples=("/upgrade", "/upgrade v<version>"),
        summary="preview a software upgrade operation",
        operation_action="preview",
        operation_target="upgrade",
    ),
    AssistantCommandSpec(
        intent_name="monitor_run_confirm",
        tool_name="inbound.monitor_run",
        commands=("/confirm",),
        display_name="确认执行监控",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        risk_level="confirm_write",
        examples=("/confirm monitor-run [operation_id]", "确认运行监控"),
        summary="confirm a pending monitor tick run preview",
        operation_action="confirm",
        operation_target="monitor_run",
        operation_target_aliases=("monitor-run", "monitor_run", "tick", "run-monitor", "运行监控", "跑监控", "执行监控"),
    ),
    AssistantCommandSpec(
        intent_name="monitor_run_cancel",
        tool_name="inbound.monitor_run",
        commands=("/cancel",),
        display_name="取消执行监控",
        arguments=("operation_id", "operation_resolution"),
        read_only=False,
        risk_level="confirm_write",
        examples=("/cancel monitor-run [operation_id]", "取消运行监控"),
        summary="cancel a pending monitor tick run preview",
        operation_action="cancel",
        operation_target="monitor_run",
        operation_target_aliases=("monitor-run", "monitor_run", "tick", "run-monitor", "运行监控", "跑监控", "执行监控"),
    ),
    AssistantCommandSpec(
        intent_name="monitor_run_now",
        tool_name="inbound.monitor_run",
        commands=("/monitor-run",),
        display_name="执行一次监控",
        arguments=("market", "accounts", "symbols", "timeout_seconds"),
        read_only=False,
        risk_level="preview_admin",
        examples=("/monitor-run hk", "跑一次港股监控", "单独跑一次 PDD 的监控"),
        summary="preview running one guarded tick-cron monitor cycle",
        operation_action="preview",
        operation_target="monitor_run",
    ),
)


def command_specs() -> tuple[AssistantCommandSpec, ...]:
    return COMMAND_SPECS


def command_catalog_payload() -> dict[str, Any]:
    specs = [_spec_payload(spec) for spec in COMMAND_SPECS]
    return {
        "summary": {
            "command_count": len(specs),
            "capability_count": len(specs),
            "slash_command_count": len({command for spec in COMMAND_SPECS for command in spec.commands}),
            "read_only_count": sum(1 for item in specs if item["read_only"]),
            "direct_executable_count": sum(1 for item in specs if item["direct_executable"]),
            "write_command_count": sum(1 for item in specs if not item["read_only"]),
            "write_capability_count": sum(1 for item in specs if not item["read_only"]),
        },
        "schema_version": COMMAND_CATALOG_SCHEMA_VERSION,
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
    reads = [item for item in capabilities if item.get("kind") in {"read", "local"}]
    previews = [item for item in capabilities if item.get("kind") == "preview"]
    applies = [item for item in capabilities if item.get("kind") == "apply"]

    lines = [
        "Deterministic Control capabilities",
        "",
        "Read and local commands:",
    ]
    lines.extend(_capability_text_line(item) for item in reads)
    lines.extend([
        "",
        "Preview commands:",
    ])
    lines.extend(_capability_text_line(item) for item in previews)
    lines.extend([
        "",
        "Confirm and cancel commands:",
    ])
    lines.extend(_capability_text_line(item) for item in applies)
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


def preview_operation_capabilities() -> tuple[dict[str, Any], ...]:
    return tuple(
        _spec_payload(spec)
        for spec in COMMAND_SPECS
        if spec.supported and spec.risk_level in {"preview_write", "preview_admin"} and spec.tool_name
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


def _spec_payload(spec: AssistantCommandSpec) -> dict[str, Any]:
    kind = _kind(spec)
    return {
        "capability_id": spec.intent_name,
        "intent_name": spec.intent_name,
        "tool_name": spec.tool_name,
        "kind": kind,
        "commands": list(spec.commands),
        "display_name": spec.display_name,
        "arguments": list(spec.arguments),
        "read_only": bool(spec.read_only),
        "supported": bool(spec.supported),
        "direct_executable": _direct_executable(spec),
        "requires_pending": _requires_pending(spec),
        "requires_confirm": _requires_confirm(spec),
        "risk_level": _risk_level(spec),
        "examples": list(spec.examples),
        "summary": spec.summary,
        "operation_action": spec.operation_action,
        "operation_target": spec.operation_target,
        "operation_target_aliases": list(spec.operation_target_aliases),
        "required_information": list(spec.required_information),
        "usage_patterns": list(spec.examples),
    }
def _kind(spec: AssistantCommandSpec) -> str:
    if spec.kind:
        return spec.kind
    risk = _risk_level(spec)
    if spec.intent_name in {"help", "small_talk"} or spec.tool_name is None:
        return "local"
    if spec.read_only and risk == "read_only":
        return "read"
    if spec.operation_action == "preview" and risk in {"preview_write", "preview_admin"}:
        return "preview"
    if spec.operation_action in {"confirm", "cancel"} or risk == "confirm_write":
        return "apply"
    return "preview" if not spec.read_only else "read"


def _direct_executable(spec: AssistantCommandSpec) -> bool:
    if spec.direct_executable is not None:
        return bool(spec.direct_executable)
    return bool(_kind(spec) in {"read", "local"} and spec.supported)


def _requires_pending(spec: AssistantCommandSpec) -> bool:
    if spec.requires_pending is not None:
        return bool(spec.requires_pending)
    return bool(_kind(spec) == "apply" or spec.operation_action in {"confirm", "cancel"})


def _requires_confirm(spec: AssistantCommandSpec) -> bool:
    if spec.requires_confirm is not None:
        return bool(spec.requires_confirm)
    return bool(_kind(spec) in {"preview", "apply"} and not spec.read_only)


def _risk_level(spec: AssistantCommandSpec) -> str:
    if spec.risk_level:
        return spec.risk_level
    return "read_only" if spec.read_only else "write"


def _capability_text_line(item: dict[str, Any]) -> str:
    commands = ", ".join(_unique(item.get("commands") or ())) or "-"
    usage = " | ".join(_unique(item.get("usage_patterns") or item.get("examples") or ())[:3]) or "-"
    arguments = ", ".join(_unique(item.get("arguments") or ())) or "-"
    executable = "true" if item.get("direct_executable") else "false"
    return (
        f"- {item.get('capability_id')} ({item.get('display_name')}): risk={item.get('risk_level')} "
        f"direct_executable={executable} commands={commands} args={arguments} usage={usage}"
    )


def command_help_text() -> str:
    specs = [_spec_payload(spec) for spec in COMMAND_SPECS]
    read_only = [item for item in specs if item["read_only"] and item.get("commands")]
    preview_writes = [
        item
        for item in specs
        if not item["read_only"] and item["risk_level"] in {"preview_write", "preview_admin"} and item.get("commands")
    ]
    confirm_shortcuts = _non_slash_examples(
        item for item in specs if not item["read_only"] and item["intent_name"].endswith("_confirm")
    )
    confirm_command = _operation_command_hint(specs, action="confirm")
    cancel_command = _operation_command_hint(specs, action="cancel")
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
    examples = [example for example in _unique(item.get("examples") or ()) if str(example).strip().startswith("/")]
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


def _operation_command_hint(items: list[dict[str, Any]], *, action: str) -> str:
    targets = _unique(
        str(item.get("operation_target") or "").strip()
        for item in items
        if str(item.get("operation_action") or "").strip() == action
        and str(item.get("operation_target") or "").strip()
    )
    if not targets:
        return ""
    command = "/confirm" if action == "confirm" else "/cancel"
    return f"{command} {'|'.join(targets)} [operation_id]"


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
