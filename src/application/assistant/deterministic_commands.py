from __future__ import annotations

import re
from datetime import date
from typing import Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.capability_catalog import command_specs, operation_specs
from src.application.assistant.contracts import PerceptionResult


_ACCOUNT_RE = re.compile(r"(?<![a-z0-9_])(lx|sy)(?![a-z0-9_])", re.IGNORECASE)
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])(?!\d)")
_OPERATION_ID_RE = re.compile(r"\bin_[A-Za-z0-9_.:-]+\b")
_VERSION_RE = re.compile(r"\bv?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?)\b")
_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z]{1,8}(?:\.[A-Za-z]{1,4})?|[A-Za-z]{2}\.\d{4,5}|\d{3,5}(?:\.HK)?|[\u4e00-\u9fff]{2,8})(?![A-Za-z0-9_.])")
_MANUAL_UPDATE_SET_RE = r"(?:改成|改为|变成|设为|设置为|调整为|调整成|to|=|:|：)"
_MANUAL_UPDATE_ALIASES: tuple[tuple[str, str], ...] = (
    ("premium_per_share", "premium_per_share"),
    ("premium", "premium_per_share"),
    ("权利金", "premium_per_share"),
    ("close_price", "close_price"),
    ("close", "close_price"),
    ("平仓价", "close_price"),
    ("平仓价格", "close_price"),
    ("contracts_to_close", "contracts_to_close"),
    ("平仓数量", "contracts_to_close"),
    ("合约数", "contracts"),
    ("数量", "contracts"),
    ("张数", "contracts"),
    ("contracts", "contracts"),
    ("qty", "contracts"),
    ("strike", "strike"),
    ("行权价", "strike"),
    ("multiplier", "multiplier"),
    ("乘数", "multiplier"),
    ("underlying_share_locked", "underlying_share_locked"),
    ("locked_shares", "underlying_share_locked"),
    ("locked", "underlying_share_locked"),
    ("锁定股数", "underlying_share_locked"),
    ("expiration_ymd", "expiration_ymd"),
    ("expiration", "expiration_ymd"),
    ("到期日", "expiration_ymd"),
    ("exp", "expiration_ymd"),
    ("option_type", "option_type"),
    ("type", "option_type"),
    ("side", "side"),
    ("方向", "side"),
    ("currency", "currency"),
    ("币种", "currency"),
    ("record_id", "record_id"),
    ("close_reason", "close_reason"),
    ("note", "note"),
    ("备注", "note"),
)
_READ_ONLY_HINT = ""


def parse_deterministic_text(text: str, *, now_fn: Callable[[], date] | None = None) -> PerceptionResult:
    raw = str(text or "").strip()
    if not raw:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="请输入要查询的内容。",
            hint=_read_only_hint(),
        )
    if raw.startswith("/"):
        from src.application.assistant.command_parser import parse_assistant_command

        command_intent = parse_assistant_command(raw, now_fn=now_fn)
        if command_intent is not None:
            return command_intent

    compact = _compact(raw)
    lower = raw.lower().strip()

    if compact in {"你好", "您好", "hi", "hello", "嗨"}:
        return PerceptionResult(intent_name="small_talk", arguments={"kind": "hello"})

    if compact in {
        "你能做什么",
        "我能做什么",
        "能做什么",
        "你会什么",
        "有什么功能",
        "有哪些功能",
        "可用功能",
        "可用命令",
        "命令",
        "指令",
        "功能",
        "菜单",
    }:
        return PerceptionResult(intent_name="help", arguments={})

    if compact in {"待确认", "当前预览", "待确认记录", "pending", "pendingoperations"} or lower in {
        "pending",
        "pending operations",
        "current preview",
    }:
        return PerceptionResult(intent_name="pending_operations", arguments={})

    operation_intent = _parse_operation_intent(raw, compact=compact, lower=lower)
    if operation_intent is not None:
        return operation_intent

    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="没有识别出明确 command 或待确认操作。",
        hint="只读查询请使用 /status、/positions、/income、/runs、/logs，或开启 assistant planner 让 LLM 选择只读工具。",
    )


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def _read_only_hint() -> str:
    global _READ_ONLY_HINT
    if _READ_ONLY_HINT:
        return _READ_ONLY_HINT
    examples: list[str] = []
    for spec in command_specs():
        if not spec.read_only or spec.intent_name == "help":
            continue
        examples.extend(command for command in spec.commands if command)
    _READ_ONLY_HINT = f"可用：{'、'.join(_unique(examples))}。"
    return _READ_ONLY_HINT


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        out.append(value)
        seen.add(value)
    return out


def _extract_account(text: str) -> str | None:
    match = _ACCOUNT_RE.search(text)
    return match.group(1).lower() if match else None


def _parse_operation_intent(text: str, *, compact: str, lower: str) -> PerceptionResult | None:
    confirm_intent = _parse_catalog_operation_reference(text, compact=compact, lower=lower, action="confirm")
    if confirm_intent is not None:
        return confirm_intent
    cancel_intent = _parse_catalog_operation_reference(text, compact=compact, lower=lower, action="cancel")
    if cancel_intent is not None:
        return cancel_intent

    if _looks_like_symbol_add(compact, lower):
        return PerceptionResult(intent_name="symbol_add", arguments=_parse_symbol_add(text))
    if _looks_like_symbol_edit(compact, lower):
        return PerceptionResult(intent_name="symbol_edit", arguments=_parse_symbol_edit(text))
    if _looks_like_symbol_remove(compact, lower):
        return PerceptionResult(intent_name="symbol_remove", arguments=_parse_symbol_remove(text))

    if _looks_like_upgrade_now(compact, lower):
        return PerceptionResult(intent_name="upgrade_now", arguments=_parse_upgrade_request(text))
    if _looks_like_manual_open(compact, lower):
        return PerceptionResult(intent_name="manual_trade_open", arguments=_parse_manual_trade_request(text))
    if _looks_like_manual_close(compact, lower):
        return PerceptionResult(intent_name="manual_trade_close", arguments=_parse_manual_trade_request(text))
    manual_update = _parse_manual_trade_update(text)
    if manual_update:
        return PerceptionResult(intent_name="manual_trade_update", arguments=manual_update)
    return None


def _parse_catalog_operation_reference(
    text: str,
    *,
    compact: str,
    lower: str,
    action: str,
) -> PerceptionResult | None:
    for spec in operation_specs(action=action):
        if _matches_operation_reference(spec, compact=compact, lower=lower, action=action):
            return PerceptionResult(intent_name=spec.intent_name, arguments=_operation_reference_args(text))
    return None


def _matches_operation_reference(spec: object, *, compact: str, lower: str, action: str) -> bool:
    for example in getattr(spec, "examples", ()):
        value = str(example or "").strip()
        if not value or value.startswith("/"):
            continue
        if compact.startswith(_compact(value)) or lower.startswith(value.lower()):
            return True

    action_words = {
        "confirm": ("confirm", "确认"),
        "cancel": ("cancel", "取消"),
    }.get(action, ())
    for alias in getattr(spec, "operation_target_aliases", ()):
        target = str(alias or "").strip().lower()
        if not target:
            continue
        if any(lower.startswith(f"{word} {target}") for word in action_words if word.isascii()):
            return True
        if any(compact.startswith(f"{word}{target}") for word in action_words if not word.isascii()):
            return True
    return False


def _operation_reference_args(text: str) -> dict[str, object]:
    operation_id = _extract_operation_id(text)
    return {
        "operation_id": operation_id,
        "operation_resolution": "explicit" if operation_id else "latest_pending",
    }


def _extract_operation_id(text: str) -> str | None:
    match = _OPERATION_ID_RE.search(text)
    if match:
        return match.group(0)
    return None


def _parse_manual_trade_request(text: str) -> dict[str, object]:
    args: dict[str, object] = {"raw_text": text}
    account = _extract_account(text)
    if account:
        args["account"] = account
    return args


def _parse_upgrade_request(text: str) -> dict[str, object]:
    args: dict[str, object] = {}
    match = _VERSION_RE.search(text)
    if match:
        args["target_version"] = match.group(1)
    return args


def _parse_manual_trade_update(text: str) -> dict[str, object]:
    updates: dict[str, object] = {}
    labeled = _extract_labeled_values(text)
    for raw_key, raw_value in labeled.items():
        field = _manual_update_field(raw_key)
        if field:
            updates[field] = _manual_update_value(field, raw_value)
    for alias, field in sorted(_MANUAL_UPDATE_ALIASES, key=lambda item: len(item[0]), reverse=True):
        pattern = rf"{re.escape(alias)}\s*{_MANUAL_UPDATE_SET_RE}\s*([^\s,，。]+)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            updates[field] = _manual_update_value(field, match.group(1))
    if not updates:
        return {}
    operation_id = _extract_operation_id(text)
    return {
        "operation_id": operation_id,
        "operation_resolution": "explicit" if operation_id else "latest_pending",
        "updates": updates,
    }


def _manual_update_field(raw_key: str) -> str | None:
    lowered = str(raw_key or "").strip().lower()
    for alias, field in _MANUAL_UPDATE_ALIASES:
        if lowered == alias.lower():
            return field
    return None


def _manual_update_value(field: str, raw_value: object) -> object:
    text = str(raw_value or "").strip()
    if field in {"contracts", "contracts_to_close", "underlying_share_locked"}:
        return _parse_int_token(text)
    if field in {"premium_per_share", "close_price", "strike", "multiplier"}:
        return _parse_float_token(text)
    if field == "expiration_ymd":
        return _parse_date(text) or text
    if field == "option_type":
        return _parse_option_type(text) or text.lower()
    if field == "side":
        return _parse_position_side(text) or text.lower()
    if field == "currency":
        return text.upper()
    return text


def _parse_int_token(text: str) -> int:
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise AgentToolError(code="INPUT_ERROR", message=f"无法解析整数：{text}")
    token = match.group(0)
    if "." in token:
        raise AgentToolError(code="INPUT_ERROR", message=f"整数参数不能写小数：{token}")
    return int(token)


def _parse_float_token(text: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise AgentToolError(code="INPUT_ERROR", message=f"无法解析数字：{text}")
    return float(match.group(0))


def _parse_date(text: str) -> str | None:
    match = _DATE_RE.search(text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def _parse_option_type(text: str) -> str | None:
    lower = text.lower()
    if "put" in lower or "看跌" in text or "沽" in text:
        return "put"
    if "call" in lower or "看涨" in text or "购" in text:
        return "call"
    return None


def _parse_position_side(text: str) -> str | None:
    lower = text.lower()
    if "short" in lower or "sell" in lower or "卖出" in text:
        return "short"
    if "long" in lower or "buy" in lower or "买入" in text:
        return "long"
    return None


def _parse_symbol_add(text: str) -> dict[str, object]:
    labeled = _extract_labeled_values(text)
    symbol = str(labeled.get("symbol") or _extract_monitor_symbol(text) or "").strip()
    lower = text.lower()
    args: dict[str, object] = {
        "symbol": symbol,
        "sell_put_enabled": "put" in lower or "sell_put" in lower or "看跌" in text,
        "sell_call_enabled": "call" in lower or "sell_call" in lower or "看涨" in text,
    }
    use = labeled.get("use")
    if use:
        args["use"] = str(use)
    limit_exp = _parse_int_value(labeled, ("limit_expirations", "limit_exp"))
    if limit_exp is not None:
        args["limit_expirations"] = limit_exp
    accounts_raw = labeled.get("accounts")
    if accounts_raw:
        args["accounts"] = [item.strip() for item in str(accounts_raw).split(",") if item.strip()]
    return {key: value for key, value in args.items() if value not in (None, "")}


def _parse_symbol_edit(text: str) -> dict[str, object]:
    labeled = _extract_labeled_values(text)
    sets = _extract_symbol_set_values(text)
    natural_sets = _extract_symbol_strategy_set_values(text)
    if natural_sets:
        for key in _symbol_strategy_shadow_keys(text):
            sets.pop(key, None)
        sets.update(natural_sets)
    args: dict[str, object] = {
        "symbol": labeled.get("symbol") or _extract_monitor_symbol(text),
        "set": sets,
    }
    ensure_use = _extract_symbol_ensure_use(text, sets)
    if ensure_use:
        args["ensure_use"] = ensure_use
    return {key: value for key, value in args.items() if value not in (None, "")}


def _parse_symbol_remove(text: str) -> dict[str, object]:
    labeled = _extract_labeled_values(text)
    return {"symbol": labeled.get("symbol") or _extract_monitor_symbol(text) or ""}


def _extract_labeled_values(text: str) -> dict[str, str]:
    aliases = {
        "account": "account",
        "账户": "account",
        "symbol": "symbol",
        "标的": "symbol",
        "type": "option_type",
        "option_type": "option_type",
        "side": "side",
        "方向": "side",
        "strike": "strike",
        "行权价": "strike",
        "exp": "exp",
        "expiration": "expiration_ymd",
        "expiration_ymd": "expiration_ymd",
        "到期日": "expiration_ymd",
        "contracts": "contracts",
        "contracts_to_close": "contracts_to_close",
        "qty": "qty",
        "数量": "contracts",
        "multiplier": "multiplier",
        "乘数": "multiplier",
        "locked": "locked",
        "locked_shares": "underlying_share_locked",
        "premium": "premium",
        "权利金": "premium",
        "close": "close",
        "close_price": "close_price",
        "record_id": "record_id",
        "currency": "currency",
        "note": "note",
        "use": "use",
        "accounts": "accounts",
        "limit_exp": "limit_exp",
        "limit_expirations": "limit_expirations",
    }
    out: dict[str, str] = {}
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,8})\s*[:=：]\s*([^\s,，]+)", text):
        key = aliases.get(match.group(1).strip().lower()) or aliases.get(match.group(1).strip())
        if key:
            out[key] = match.group(2).strip()
    return out


def _extract_symbol(text: str) -> str | None:
    skip = {
        "记录",
        "记录开仓",
        "记录平仓",
        "开仓",
        "平仓",
        "确认记录",
        "取消记录",
        "short",
        "long",
        "sell",
        "buy",
        "put",
        "call",
        "strike",
        "exp",
        "premium",
        "multiplier",
        "close",
        "record_id",
        "covered",
        "covered_call",
        "sell_call",
        "sell_put",
        "min",
        "max",
        "setting",
        "settings",
        "设置",
        "配置",
        "修改",
        "调整",
        "lx",
        "sy",
    }
    for match in _SYMBOL_RE.finditer(text):
        raw = match.group(1).strip()
        if not raw:
            continue
        lowered = raw.lower()
        if lowered in skip or _DATE_RE.fullmatch(raw) or raw.startswith("in_"):
            continue
        if raw.isdigit() and len(raw) < 3:
            continue
        return raw
    return None


def _extract_monitor_symbol(text: str) -> str | None:
    cleaned = re.sub(r"^(查看|增加|新增|修改|配置|设置|删除|移除)?(?:监控)?标的", "", text.strip(), flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(symbols?|symbol)\s+(list|add|edit|remove|rm)\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^[\s,，:：。]*为?", "", cleaned).strip()
    return _extract_symbol(cleaned)


def _extract_symbol_set_values(text: str) -> dict[str, object]:
    out: dict[str, object] = {}
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.]*)=([^\s,，]+)", text):
        key = match.group(1).strip()
        if key in {"symbol", "account", "record_id"}:
            continue
        if key.startswith("covered_call."):
            key = f"sell_call.{key.removeprefix('covered_call.')}"
        out[key] = _parse_scalar(match.group(2))
    return out


def _extract_symbol_strategy_set_values(text: str) -> dict[str, object]:
    out: dict[str, object] = {}
    sell_call_context = _mentions_sell_call_context(text)
    sell_put_context = _mentions_sell_put_context(text)
    if _mentions_sell_call_enable_context(text):
        out["sell_call.enabled"] = not _mentions_disabled_context(text)
    for bound, aliases in (
        ("min_strike", ("min[_\\s-]*strike", "最低行权价", "最小行权价")),
        ("max_strike", ("max[_\\s-]*strike", "最高行权价", "最大行权价")),
    ):
        value = _extract_first_strategy_number(text, aliases)
        if value is None:
            continue
        if sell_call_context:
            out[f"sell_call.{bound}"] = value
        elif sell_put_context:
            out[f"sell_put.{bound}"] = value
    return out


def _extract_first_strategy_number(text: str, aliases: tuple[str, ...]) -> float | None:
    for alias in aliases:
        match = re.search(
            rf"(?:{alias})\s*(?:=|:|：|设为|设置为|改为|改成|调整为|调整成|to)?\s*(-?\d+(?:\.\d+)?)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return float(match.group(1))
    return None


def _symbol_strategy_shadow_keys(text: str) -> set[str]:
    keys: set[str] = set()
    if re.search(r"min[_\s-]*strike\s*(?:=|:|：)", text, flags=re.IGNORECASE):
        keys.update({"strike", "min_strike"})
    if re.search(r"max[_\s-]*strike\s*(?:=|:|：)", text, flags=re.IGNORECASE):
        keys.update({"strike", "max_strike"})
    return keys


def _extract_symbol_ensure_use(text: str, sets: dict[str, object]) -> list[str]:
    if _mentions_disabled_context(text) and sets.get("sell_call.enabled") is False:
        return []
    if _mentions_sell_call_context(text) or any(str(key).startswith("sell_call.") for key in sets):
        return ["call_base"]
    return []


def _mentions_sell_call_context(text: str) -> bool:
    lower = text.lower()
    compact = _compact(text).lower()
    return any(token in lower for token in ("covered call", "covered_call", "sell call", "sell_call")) or "备兑" in text or "coveredcall" in compact


def _mentions_sell_call_enable_context(text: str) -> bool:
    lower = text.lower()
    compact = _compact(text).lower()
    return any(token in lower for token in ("covered call", "covered_call", "sell call")) or "备兑" in text or "coveredcall" in compact


def _mentions_sell_put_context(text: str) -> bool:
    lower = text.lower()
    return "sell put" in lower or "sell_put" in lower or "现金担保" in text or "卖沽" in text


def _mentions_disabled_context(text: str) -> bool:
    lower = text.lower()
    compact = _compact(text)
    return any(token in lower for token in ("false", "off", "disable", "disabled")) or any(token in compact for token in ("关闭", "禁用", "停用"))


def _parse_int_value(values: dict[str, str], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        raw = values.get(key)
        if raw not in (None, ""):
            return int(float(str(raw)))
    return None


def _parse_scalar(raw: str) -> object:
    value = str(raw or "").strip()
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        return float(value) if "." in value else int(value)
    except Exception:
        return value


def _looks_like_upgrade_now(compact: str, lower: str) -> bool:
    return compact.startswith("立即升级") or compact in {"升级", "马上升级"} or lower in {"upgrade now", "update now"} or lower.startswith("upgrade now ")


def _looks_like_manual_open(compact: str, lower: str) -> bool:
    return compact.startswith("记录开仓") or lower.startswith("record open") or lower.startswith("trade open")


def _looks_like_manual_close(compact: str, lower: str) -> bool:
    return compact.startswith("记录平仓") or lower.startswith("record close") or lower.startswith("trade close")


def _looks_like_symbol_add(compact: str, lower: str) -> bool:
    return compact.startswith("增加监控标的") or compact.startswith("新增监控标的") or lower.startswith("symbol add ") or lower.startswith("symbols add ")


def _looks_like_symbol_edit(compact: str, lower: str) -> bool:
    return (
        compact.startswith("修改监控标的")
        or compact.startswith("配置监控标的")
        or compact.startswith("配置标的")
        or compact.startswith("设置监控标的")
        or lower.startswith("symbol edit ")
        or lower.startswith("symbols edit ")
        or _looks_like_strategy_symbol_edit(lower)
    )


def _looks_like_symbol_remove(compact: str, lower: str) -> bool:
    return compact.startswith("删除监控标的") or compact.startswith("移除监控标的") or lower.startswith("symbols rm ")


def _looks_like_strategy_symbol_edit(text: str) -> bool:
    if "record open" in text or "record close" in text or "记录开仓" in text or "记录平仓" in text:
        return False
    natural_sets = _extract_symbol_strategy_set_values(text)
    if not natural_sets:
        return False
    return bool(_extract_monitor_symbol(text))
