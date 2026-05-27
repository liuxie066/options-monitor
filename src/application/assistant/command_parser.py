from __future__ import annotations

import re
import shlex
from datetime import date
from typing import Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.commands import commands_by_intent, operation_target_intents
from src.application.assistant.contracts import AssistantIntent


_MONTH_RE = re.compile(r"^(20\d{2})[-/.](0[1-9]|1[0-2])$")
_OPERATION_ID_RE = re.compile(r"^in_[A-Za-z0-9_.:-]+$")
_ACCOUNTS = frozenset({"lx", "sy"})
_COMMANDS = commands_by_intent()
_CONFIRM_TARGETS = operation_target_intents("confirm")
_CANCEL_TARGETS = operation_target_intents("cancel")


def parse_assistant_command(text: str, *, now_fn: Callable[[], date] | None = None) -> AssistantIntent | None:
    raw = str(text or "").strip()
    if not raw.startswith("/"):
        return None
    parts = _split_command(raw)
    command = parts[0].lower()
    args = parts[1:]
    today = now_fn() if now_fn is not None else date.today()

    if command in _COMMANDS["help"]:
        return _intent("help")
    if command in _COMMANDS["runtime_status"]:
        _reject_extra(command, args)
        return _intent("runtime_status")
    if command in _COMMANDS["healthcheck"]:
        _reject_extra(command, args)
        return _intent("healthcheck")
    if command in _COMMANDS["config_validate"]:
        _reject_extra(command, args)
        return _intent("config_validate")
    if command in _COMMANDS["option_positions_open"]:
        return _parse_positions(command, args)
    if command in _COMMANDS["monthly_income_report"]:
        return _parse_income(command, args, today=today)
    if command in _COMMANDS["runtime_runs"]:
        return _parse_runs(command, args)
    if command in _COMMANDS["runtime_logs"]:
        return _parse_logs(command, args)
    if command in _COMMANDS["symbol_list"]:
        _reject_extra(command, args)
        return _intent("symbol_list")
    if command in _COMMANDS["pending_operations"]:
        _reject_extra(command, args)
        return _intent("pending_operations")
    if command in _COMMANDS["manual_trade_open"]:
        return _parse_manual_trade_preview_command(
            command,
            args,
            intent_name="manual_trade_open",
            action_prefix="记录开仓",
            hint="支持：/record-open lx NVDA short put strike 100 exp 2026-06-19 1张 premium 2.5 multiplier 100。",
        )
    if command in _COMMANDS["manual_trade_close"]:
        return _parse_manual_trade_preview_command(
            command,
            args,
            intent_name="manual_trade_close",
            action_prefix="记录平仓",
            hint="支持：/record-close record_id=<record_id> 1张 close 0.8。",
        )
    if command in _COMMANDS["manual_trade_confirm"]:
        return _parse_operation_command(command, args, target_map=_CONFIRM_TARGETS, action_label="确认")
    if command in _COMMANDS["manual_trade_cancel"]:
        return _parse_operation_command(command, args, target_map=_CANCEL_TARGETS, action_label="取消")

    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message=f"未知 command：{command}",
        hint="使用 /help 查看支持的 command。",
        details={"command": command},
    )


parse_agent_command = parse_assistant_command


def _intent(name: str, arguments: dict[str, object] | None = None) -> AssistantIntent:
    return AssistantIntent(name=name, arguments=dict(arguments or {}), parser="command", confidence=1.0)


def _split_command(raw: str) -> list[str]:
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="command 格式不完整。", hint=str(exc)) from exc
    if not parts:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="请输入 command，例如 /help。")
    return parts


def _parse_positions(command: str, args: list[str]) -> AssistantIntent:
    account: str | None = None
    status = "open"
    for arg in args:
        normalized = arg.lower()
        if normalized in _ACCOUNTS:
            if account is not None and account != normalized:
                raise _bad_arg(command, arg, "只能指定一个账号：lx 或 sy。")
            account = normalized
        elif normalized in {"all", "全部"}:
            status = "all"
        elif normalized in {"open", "持仓", "open-only"}:
            status = "open"
        else:
            raise _bad_arg(command, arg, "支持：/positions、/positions sy、/positions all。")
    payload: dict[str, object] = {"status": status}
    if account:
        payload["account"] = account
    return _intent("option_positions_open", payload)


def _parse_manual_trade_preview_command(
    command: str,
    args: list[str],
    *,
    intent_name: str,
    action_prefix: str,
    hint: str,
) -> AssistantIntent:
    if not args:
        raise _bad_arg(command, "", hint)
    raw_text = f"{action_prefix} {' '.join(args)}"
    return _intent(intent_name, {"raw_text": raw_text})


def _parse_income(command: str, args: list[str], *, today: date) -> AssistantIntent:
    account: str | None = None
    month: str | None = None
    for arg in args:
        normalized = arg.lower()
        if normalized in _ACCOUNTS:
            if account is not None and account != normalized:
                raise _bad_arg(command, arg, "只能指定一个账号：lx 或 sy。")
            account = normalized
        elif normalized in {"all", "全部"}:
            continue
        elif normalized in {"本月", "this-month"}:
            month = today.strftime("%Y-%m")
        elif normalized in {"上月", "last-month"}:
            month = _previous_month(today)
        elif _MONTH_RE.match(normalized):
            month = normalized.replace("/", "-").replace(".", "-")
        else:
            raise _bad_arg(command, arg, "支持：/income、/income sy、/income sy 2026-05、/income 上月。")
    payload: dict[str, object] = {}
    if account:
        payload["account"] = account
    if month:
        payload["month"] = month
    return _intent("monthly_income_report", payload)


def _parse_runs(command: str, args: list[str]) -> AssistantIntent:
    if not args:
        return _intent("runtime_runs", {"limit": 10})
    if len(args) != 1:
        raise _bad_arg(command, " ".join(args), "支持：/runs 或 /runs 20。")
    try:
        limit = int(args[0])
    except ValueError as exc:
        raise _bad_arg(command, args[0], "limit 必须是整数。") from exc
    return _intent("runtime_runs", {"limit": max(1, min(limit, 50))})


def _parse_logs(command: str, args: list[str]) -> AssistantIntent:
    if len(args) != 1:
        raise _bad_arg(command, " ".join(args), "支持：/logs <run_id>。")
    return _intent("runtime_logs", {"run_id": args[0], "kind": "all", "lines": 50})


def _parse_operation_command(
    command: str,
    args: list[str],
    *,
    target_map: dict[str, str],
    action_label: str,
) -> AssistantIntent:
    if not args:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"请指定要{action_label}的操作类型。",
            hint=f"示例：{command} trade in_xxx、{command} symbol in_xxx、{command} upgrade in_xxx。",
        )
    target = args[0].lower()
    if _OPERATION_ID_RE.match(args[0]):
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"请指定这个 operation_id 属于哪类操作后再{action_label}。",
            hint=f"示例：{command} trade {args[0]}、{command} symbol {args[0]}、{command} upgrade {args[0]}。",
        )
    intent_name = target_map.get(target)
    if not intent_name:
        raise _bad_arg(command, args[0], "操作类型只支持 trade、symbol、upgrade。")
    if len(args) > 2:
        raise _bad_arg(command, " ".join(args[2:]), f"支持：{command} {args[0]} 或 {command} {args[0]} in_xxx。")
    operation_id = args[1] if len(args) == 2 else None
    if operation_id and not _OPERATION_ID_RE.match(operation_id):
        raise _bad_arg(command, operation_id, "operation_id 应形如 in_xxx。")
    return _intent(
        intent_name,
        {
            "operation_id": operation_id,
            "operation_resolution": "explicit" if operation_id else "latest_pending",
        },
    )


def _previous_month(today: date) -> str:
    year = today.year
    month = today.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def _reject_extra(command: str, args: list[str]) -> None:
    if args:
        raise _bad_arg(command, " ".join(args), f"{command} 不接受额外参数。")


def _bad_arg(command: str, arg: str, hint: str) -> AgentToolError:
    return AgentToolError(
        code="NEEDS_CLARIFICATION",
        message=f"{command} 参数无法识别：{arg}",
        hint=hint,
    )
