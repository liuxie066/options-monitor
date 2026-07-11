from __future__ import annotations

import re
import shlex
from datetime import date
from typing import Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.capability_catalog import commands_by_intent, operation_target_intents
from src.application.assistant.contracts import ControlCommand
from src.application.assistant.position_query import parse_position_query_text, position_query_intent_arguments


_MONTH_RE = re.compile(r"^(20\d{2})[-/.](0[1-9]|1[0-2])$")
_YEAR_MONTH_CN_RE = re.compile(r"^(20\d{2})年(1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月$")
_MONTH_CN_RE = re.compile(r"^(1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月$")
_OPERATION_ID_RE = re.compile(r"^in_[A-Za-z0-9_.:-]+$")
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?)$")
_ACCOUNTS = frozenset({"lx", "sy"})
_COMMANDS = commands_by_intent()
_CONFIRM_TARGETS = operation_target_intents("confirm")
_CANCEL_TARGETS = operation_target_intents("cancel")
_CN_MONTHS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


def parse_assistant_command(text: str, *, now_fn: Callable[[], date] | None = None) -> ControlCommand | None:
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
    if command in _COMMANDS["position_query"]:
        return _parse_positions(command, args, today=today)
    if command in _COMMANDS["assigned_stock_position_query"]:
        return _parse_assigned_stock(command, args)
    if command in _COMMANDS["monthly_income_report"]:
        return _parse_income(command, args, today=today)
    if command in _COMMANDS["runtime_runs"]:
        return _parse_runs(command, args)
    if command in _COMMANDS["runtime_logs"]:
        return _parse_logs(command, args)
    if command in _COMMANDS["symbol_list"] or command in _COMMANDS["symbol_add"]:
        return _parse_symbol_command(command, args)
    if command in _COMMANDS["pending_operations"]:
        _reject_extra(command, args)
        return _intent("pending_operations")
    if command in _COMMANDS["model_list"] or command in _COMMANDS["model_use"]:
        return _parse_model_command(command, args)
    if command in _COMMANDS["manual_trade_open"]:
        return _parse_manual_trade_preview_command(
            command,
            args,
            intent_name="manual_trade_open",
            action_prefix="记录开仓",
            hint="格式：/record-open [账户] <标的> <short|long> <put|call> strike <行权价> exp <YYYY-MM-DD> <张数>张 premium <权利金> multiplier <乘数>。",
        )
    if command in _COMMANDS["manual_trade_close"]:
        return _parse_manual_trade_preview_command(
            command,
            args,
            intent_name="manual_trade_close",
            action_prefix="记录平仓",
            hint="格式：/record-close record_id=<record_id> <张数>张 close <平仓价>。",
        )
    if command in _COMMANDS["manual_expiry"]:
        return _parse_manual_trade_preview_command(
            command,
            args,
            intent_name="manual_expiry",
            action_prefix="记录到期失效",
            hint="格式：/record-expiry <富途期权到期失效通知>。",
        )
    if command in _COMMANDS["manual_trade_update"]:
        return _parse_manual_trade_update_command(command, args)
    if command in _COMMANDS["manual_trade_confirm"]:
        return _parse_operation_command(command, args, target_map=_CONFIRM_TARGETS, action_label="确认")
    if command in _COMMANDS["manual_trade_cancel"]:
        return _parse_operation_command(command, args, target_map=_CANCEL_TARGETS, action_label="取消")
    if command in _COMMANDS["upgrade_now"]:
        return _parse_upgrade_command(command, args)
    if command in _COMMANDS["monitor_run_now"]:
        return _parse_monitor_run_command(command, args)

    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message=f"未知 command：{command}",
        hint="使用 /help 查看支持的 command。",
        details={"command": command},
    )


parse_agent_command = parse_assistant_command


def _intent(name: str, arguments: dict[str, object] | None = None) -> ControlCommand:
    return ControlCommand(intent_name=name, arguments=dict(arguments or {}), source="command", confidence=1.0)


def _split_command(raw: str) -> list[str]:
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="command 格式不完整。", hint=str(exc)) from exc
    if not parts:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="请输入 command，例如 /help。")
    return parts


def _parse_positions(command: str, args: list[str], *, today: date) -> ControlCommand:
    raw = "持仓" if not args else f"持仓 {' '.join(args)}"
    query = parse_position_query_text(raw, today=today)
    return _intent("position_query", position_query_intent_arguments(query))


def _parse_assigned_stock(command: str, args: list[str]) -> ControlCommand:
    account: str | None = None
    status: str = "open"
    symbol: str | None = None
    stock_lot_id: str | None = None
    refresh_quotes = True
    for arg in args:
        normalized = arg.lower()
        if normalized in _ACCOUNTS:
            if account is not None and account != normalized:
                raise _bad_arg(command, arg, "只能指定一个账号：lx 或 sy。")
            account = normalized
        elif normalized in {"all", "全部"}:
            status = "all"
        elif normalized in {"open", "持仓", "未卖出"}:
            status = "open"
        elif normalized in {"partially_sold", "partial", "partially-sold", "部分卖出"}:
            status = "partially_sold"
        elif normalized in {"closed", "close", "已卖出", "已关闭"}:
            status = "closed"
        elif normalized in {"no-refresh", "no_refresh", "offline"}:
            refresh_quotes = False
        elif normalized.startswith("stock_lot_id="):
            stock_lot_id = arg.split("=", 1)[1].strip() or None
        elif symbol is None:
            symbol = arg.upper()
        else:
            raise _bad_arg(
                command,
                arg,
                "支持：/assigned-stock [lx|sy|all] [symbol] [open|partially_sold|closed|all] [no-refresh]。",
            )
    payload: dict[str, object] = {
        "assigned_stock_status": status,
        "refresh_quotes": refresh_quotes,
    }
    if account:
        payload["account"] = account
    if symbol:
        payload["symbol"] = symbol
    if stock_lot_id:
        payload["stock_lot_id"] = stock_lot_id
    return _intent("assigned_stock_position_query", payload)


def _parse_manual_trade_preview_command(
    command: str,
    args: list[str],
    *,
    intent_name: str,
    action_prefix: str,
    hint: str,
) -> ControlCommand:
    if not args:
        raise _bad_arg(command, "", hint)
    raw_text = f"{action_prefix} {' '.join(args)}"
    return _intent(intent_name, {"raw_text": raw_text})


def _parse_symbol_command(command: str, args: list[str]) -> ControlCommand:
    if not args:
        return _intent("symbol_list")
    action = args[0].lower()
    if action in {"list", "ls", "show"}:
        if len(args) != 1:
            raise _bad_arg(command, " ".join(args[1:]), "支持：/symbols、/symbol add <symbol>、/symbol edit <symbol> <field>=<value>、/symbol remove <symbol>。")
        return _intent("symbol_list")
    if action in {"add", "new"}:
        if len(args) < 2:
            raise _bad_arg(command, "", "格式：/symbol add <symbol> [put|call] [use=<name>] [limit_exp=<n>]。")
        return _intent("symbol_add", _symbol_add_args(args[1:]))
    if action in {"edit", "set"}:
        if len(args) < 3:
            raise _bad_arg(command, "", "格式：/symbol edit <symbol> <field>=<value> [field=value ...]。")
        return _intent("symbol_edit", _symbol_edit_args(args[1:]))
    if action in {"remove", "rm", "delete", "del"}:
        if len(args) != 2:
            raise _bad_arg(command, " ".join(args[1:]), "格式：/symbol remove <symbol>。")
        return _intent("symbol_remove", {"symbol": args[1]})
    raise _bad_arg(command, args[0], "支持：/symbols、/symbol add、/symbol edit、/symbol remove。")


def _symbol_add_args(args: list[str]) -> dict[str, object]:
    symbol = args[0]
    out: dict[str, object] = {
        "symbol": symbol,
        "sell_put_enabled": False,
        "sell_call_enabled": False,
    }
    accounts: list[str] = []
    for raw in args[1:]:
        normalized = raw.lower()
        if normalized in {"put", "sell_put"}:
            out["sell_put_enabled"] = True
        elif normalized in {"call", "sell_call", "covered_call"}:
            out["sell_call_enabled"] = True
        elif "=" in raw:
            key, value = _split_key_value(raw)
            if key == "use":
                out["use"] = value
            elif key in {"limit_exp", "limit_expirations"}:
                out["limit_expirations"] = _positive_int(value, key)
            elif key == "accounts":
                accounts.extend(item.strip() for item in value.split(",") if item.strip())
            else:
                raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"无法识别 symbol add 参数：{raw}", hint="格式：/symbol add <symbol> [put|call] [use=<name>] [limit_exp=<n>]。")
        else:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"无法识别 symbol add 参数：{raw}", hint="格式：/symbol add <symbol> [put|call] [use=<name>] [limit_exp=<n>]。")
    if accounts:
        out["accounts"] = accounts
    return out


def _symbol_edit_args(args: list[str]) -> dict[str, object]:
    symbol = args[0]
    values: dict[str, object] = {}
    ensure_use: list[str] = []
    for raw in args[1:]:
        key, value = _split_key_value(raw)
        if key == "ensure_use":
            ensure_use.extend(item.strip() for item in value.split(",") if item.strip())
            continue
        values[key] = _parse_scalar(value)
    out: dict[str, object] = {"symbol": symbol, "set": values}
    if ensure_use:
        out["ensure_use"] = ensure_use
    return out


def _parse_upgrade_command(command: str, args: list[str]) -> ControlCommand:
    if len(args) > 2:
        raise _bad_arg(command, " ".join(args), "支持：/upgrade 或 /upgrade v<version>。")
    tokens = [arg for arg in args if arg.lower() not in {"now", "check"}]
    if len(tokens) > 1:
        raise _bad_arg(command, " ".join(args), "支持：/upgrade 或 /upgrade v<version>。")
    payload: dict[str, object] = {}
    if tokens:
        match = _VERSION_RE.match(tokens[0])
        if not match:
            raise _bad_arg(command, tokens[0], "target version 必须类似 v1.2.345。")
        payload["target_version"] = match.group(1)
    return _intent("upgrade_now", payload)


def _parse_monitor_run_command(command: str, args: list[str]) -> ControlCommand:
    if not args:
        raise _bad_arg(command, "", "格式：/monitor-run hk [accounts=lx,sy] [timeout=600]。")
    market: str | None = None
    accounts: list[str] = []
    timeout_seconds: int | None = None
    for raw in args:
        normalized = raw.lower()
        if normalized in {"hk", "港股", "香港"}:
            if market is not None and market != "hk":
                raise _bad_arg(command, raw, "只能指定一个市场：hk 或 us。")
            market = "hk"
        elif normalized in {"us", "usa", "美股", "美国"}:
            if market is not None and market != "us":
                raise _bad_arg(command, raw, "只能指定一个市场：hk 或 us。")
            market = "us"
        elif normalized in _ACCOUNTS:
            accounts.append(normalized)
        elif "=" in raw:
            key, value = _split_key_value(raw)
            if key == "accounts":
                accounts.extend(item.strip().lower() for item in re.split(r"[\s,，]+", value) if item.strip())
            elif key in {"timeout", "timeout_seconds"}:
                timeout_seconds = _positive_int(value, key)
            else:
                raise _bad_arg(command, raw, "支持：/monitor-run hk [accounts=lx,sy] [timeout=600]。")
        else:
            raise _bad_arg(command, raw, "支持：/monitor-run hk [accounts=lx,sy] [timeout=600]。")
    if market is None:
        raise _bad_arg(command, "", "请明确市场：hk/港股 或 us/美股。")
    payload: dict[str, object] = {"market": market}
    if accounts:
        payload["accounts"] = accounts
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    return _intent("monitor_run_now", payload)


def _parse_manual_trade_update_command(command: str, args: list[str]) -> ControlCommand:
    if not args:
        raise _bad_arg(command, "", "格式：/record-update <field>=<value> [operation_id]。")
    operation_id: str | None = None
    updates: dict[str, object] = {}
    for raw in args:
        if _OPERATION_ID_RE.match(raw):
            operation_id = raw
            continue
        key, value = _split_key_value(raw)
        updates[key] = _parse_scalar(value)
    if not updates:
        raise _bad_arg(command, "", "至少提供一个 field=value。")
    return _intent(
        "manual_trade_update",
        {
            "operation_id": operation_id,
            "operation_resolution": "explicit" if operation_id else "latest_pending",
            "updates": updates,
        },
    )


def _parse_income(command: str, args: list[str], *, today: date) -> ControlCommand:
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
        elif match := _YEAR_MONTH_CN_RE.match(normalized):
            month_number = _month_number(match.group(2))
            if month_number is not None:
                month = f"{int(match.group(1)):04d}-{month_number:02d}"
        elif match := _MONTH_CN_RE.match(normalized):
            month_number = _month_number(match.group(1))
            if month_number is not None:
                month = f"{today.year:04d}-{month_number:02d}"
        else:
            raise _bad_arg(command, arg, "支持：/income、/income sy、/income sy 2026-05、/income sy 6月、/income 上月。")
    payload: dict[str, object] = {}
    if account:
        payload["account"] = account
    if month:
        payload["month"] = month
    return _intent("monthly_income_report", payload)


def _parse_runs(command: str, args: list[str]) -> ControlCommand:
    if not args:
        return _intent("runtime_runs", {"limit": 10})
    if len(args) != 1:
        raise _bad_arg(command, " ".join(args), "支持：/runs 或 /runs 20。")
    try:
        limit = int(args[0])
    except ValueError as exc:
        raise _bad_arg(command, args[0], "limit 必须是整数。") from exc
    return _intent("runtime_runs", {"limit": max(1, min(limit, 50))})


def _parse_logs(command: str, args: list[str]) -> ControlCommand:
    if len(args) != 1:
        raise _bad_arg(command, " ".join(args), "支持：/logs <run_id>。")
    return _intent("runtime_logs", {"run_id": args[0], "kind": "all", "lines": 50})


def _parse_model_command(command: str, args: list[str]) -> ControlCommand:
    if not args:
        return _intent("model_list")
    action = args[0].lower()
    if action in {"list", "ls", "current", "show"}:
        if len(args) != 1:
            raise _bad_arg(command, " ".join(args[1:]), "支持：/model、/model list、/model use <name>。")
        return _intent("model_list", {"view": "current" if action in {"current", "show"} else "list"})
    if action == "use":
        if len(args) != 2:
            raise _bad_arg(command, " ".join(args[1:]), "支持：/model use <name>。")
        return _intent("model_use", {"model_profile": args[1]})
    raise _bad_arg(command, args[0], "支持：/model、/model list、/model use <name>。")


def _parse_operation_command(
    command: str,
    args: list[str],
    *,
    target_map: dict[str, str],
    action_label: str,
) -> ControlCommand:
    if not args:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"请指定要{action_label}的操作类型。",
            hint=f"示例：{command} trade in_xxx、{command} symbol in_xxx、{command} upgrade in_xxx、{command} model in_xxx、{command} monitor-run in_xxx。",
        )
    target = args[0].lower()
    if _OPERATION_ID_RE.match(args[0]):
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"请指定这个 operation_id 属于哪类操作后再{action_label}。",
            hint=f"示例：{command} trade {args[0]}、{command} symbol {args[0]}、{command} upgrade {args[0]}、{command} monitor-run {args[0]}。",
        )
    intent_name = target_map.get(target)
    if not intent_name:
        raise _bad_arg(command, args[0], "操作类型只支持 trade、symbol、upgrade、model、monitor-run。")
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


def _split_key_value(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"参数需要使用 key=value：{raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key or not value:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"参数需要使用 key=value：{raw}")
    return key, value


def _parse_scalar(value: str) -> object:
    lower = value.lower()
    if lower in {"true", "yes", "on", "1"}:
        return True
    if lower in {"false", "no", "off", "0"}:
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=f"{name} must be an integer") from exc
    if parsed <= 0:
        raise AgentToolError(code="INPUT_ERROR", message=f"{name} must be positive")
    return parsed


def _previous_month(today: date) -> str:
    year = today.year
    month = today.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def _month_number(raw: str) -> int | None:
    if raw.isdigit():
        value = int(raw)
    else:
        value = _CN_MONTHS.get(raw)
    return value if value is not None and 1 <= value <= 12 else None


def _reject_extra(command: str, args: list[str]) -> None:
    if args:
        raise _bad_arg(command, " ".join(args), f"{command} 不接受额外参数。")


def _bad_arg(command: str, arg: str, hint: str) -> AgentToolError:
    return AgentToolError(
        code="NEEDS_CLARIFICATION",
        message=f"{command} 参数无法识别：{arg}",
        hint=hint,
    )
