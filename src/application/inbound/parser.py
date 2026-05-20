from __future__ import annotations

import re
from datetime import date
from typing import Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.inbound.contracts import InboundIntent


_ACCOUNT_RE = re.compile(r"(?<![a-z0-9_])(lx|sy)(?![a-z0-9_])", re.IGNORECASE)
_MONTH_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])(?!\d)")
_INT_RE = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])(?!\d)")
_OPERATION_ID_RE = re.compile(r"\bin_[A-Za-z0-9_.:-]+\b")
_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z]{1,8}(?:\.[A-Za-z]{1,4})?|[A-Za-z]{2}\.\d{4,5}|\d{3,5}(?:\.HK)?|[\u4e00-\u9fff]{2,8})(?![A-Za-z0-9_.])")


def parse_inbound_text(text: str, *, now_fn: Callable[[], date] | None = None) -> InboundIntent:
    raw = str(text or "").strip()
    if not raw:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="请输入要查询的内容。",
            hint="可用：状态、健康检查、持仓 sy、收益 sy 2026-05、最近运行、日志 <run_id>。",
        )

    compact = _compact(raw)
    lower = raw.lower().strip()
    today = now_fn() if now_fn is not None else date.today()

    if compact in {"帮助", "help", "/help"}:
        return InboundIntent(name="help", arguments={})

    operation_intent = _parse_operation_intent(raw, compact=compact, lower=lower)
    if operation_intent is not None:
        return operation_intent

    if compact in {"状态", "运行状态", "系统状态", "status"} or lower in {"status", "runtime status"}:
        return InboundIntent(name="runtime_status", arguments={})

    if "健康检查" in compact or compact in {"健康", "检查", "healthcheck", "doctor"} or lower in {"healthcheck", "doctor"}:
        return InboundIntent(name="healthcheck", arguments={})

    if "配置检查" in compact or "配置校验" in compact or lower in {"config validate", "config_validate"}:
        return InboundIntent(name="config_validate", arguments={})

    if _looks_like_positions(compact, lower):
        account = _extract_account(raw)
        if not account:
            raise AgentToolError(
                code="NEEDS_CLARIFICATION",
                message="请指定账户，例如：持仓 sy。",
                hint="当前支持账户标签 lx 或 sy。",
            )
        status = "all" if ("全部" in compact or "all" in lower) else "open"
        return InboundIntent(name="option_positions_open", arguments={"account": account, "status": status})

    if _looks_like_income(compact, lower):
        account = _extract_account(raw)
        if not account:
            raise AgentToolError(
                code="NEEDS_CLARIFICATION",
                message="请指定账户，例如：收益 sy 或 收益 sy 2026-05。",
                hint="当前支持账户标签 lx 或 sy。",
            )
        month = _extract_month(raw, compact=compact, today=today)
        args = {"account": account}
        if month:
            args["month"] = month
        return InboundIntent(name="monthly_income_report", arguments=args)

    if _looks_like_runs(compact, lower):
        limit = _extract_limit(raw, default=10, maximum=50)
        return InboundIntent(name="runtime_runs", arguments={"limit": limit})

    if _looks_like_logs(compact, lower):
        run_id = _extract_run_id_for_logs(raw)
        if not run_id:
            raise AgentToolError(
                code="NEEDS_CLARIFICATION",
                message="请指定 run_id，例如：日志 20260515T182459Z-474761。",
            )
        return InboundIntent(name="runtime_logs", arguments={"run_id": run_id, "kind": "all", "lines": 50})

    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="没有识别出可执行的只读命令。",
        hint="可用：状态、健康检查、持仓 sy、收益 sy 2026-05、最近运行、日志 <run_id>。",
    )


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def _extract_account(text: str) -> str | None:
    match = _ACCOUNT_RE.search(text)
    return match.group(1).lower() if match else None


def _extract_month(text: str, *, compact: str, today: date) -> str | None:
    match = _MONTH_RE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    if "本月" in compact or "这个月" in compact:
        return today.strftime("%Y-%m")
    if "上月" in compact or "上个月" in compact:
        year = today.year
        month = today.month - 1
        if month == 0:
            year -= 1
            month = 12
        return f"{year:04d}-{month:02d}"
    return None


def _extract_limit(text: str, *, default: int, maximum: int) -> int:
    match = _INT_RE.search(text)
    if not match:
        return default
    return max(1, min(int(match.group(1)), maximum))


def _extract_run_id_for_logs(text: str) -> str | None:
    parts = [part.strip() for part in re.split(r"\s+", text.strip()) if part.strip()]
    if len(parts) >= 2 and parts[0].lower() in {"日志", "log", "logs"}:
        return parts[1]
    match = re.search(r"日志[:：]?\s*([A-Za-z0-9_.:-]+)", text)
    if match:
        return match.group(1)
    match = re.search(r"\blogs?\s+([A-Za-z0-9_.:-]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _parse_operation_intent(text: str, *, compact: str, lower: str) -> InboundIntent | None:
    if compact.startswith("确认记录") or lower.startswith("confirm trade"):
        return InboundIntent(name="manual_trade_confirm", arguments={"operation_id": _extract_operation_id(text)})
    if compact.startswith("取消记录") or lower.startswith("cancel trade"):
        return InboundIntent(name="manual_trade_cancel", arguments={"operation_id": _extract_operation_id(text)})
    if compact.startswith("确认监控") or lower.startswith("confirm symbol"):
        return InboundIntent(name="symbol_confirm", arguments={"operation_id": _extract_operation_id(text)})
    if compact.startswith("取消监控") or lower.startswith("cancel symbol"):
        return InboundIntent(name="symbol_cancel", arguments={"operation_id": _extract_operation_id(text)})

    if _looks_like_symbol_list(compact, lower):
        return InboundIntent(name="symbol_list", arguments={})
    if _looks_like_symbol_add(compact, lower):
        return InboundIntent(name="symbol_add", arguments=_parse_symbol_add(text))
    if _looks_like_symbol_edit(compact, lower):
        return InboundIntent(name="symbol_edit", arguments=_parse_symbol_edit(text))
    if _looks_like_symbol_remove(compact, lower):
        return InboundIntent(name="symbol_remove", arguments=_parse_symbol_remove(text))

    if _looks_like_manual_open(compact, lower):
        return InboundIntent(name="manual_trade_open", arguments=_parse_manual_trade_request(text))
    if _looks_like_manual_close(compact, lower):
        return InboundIntent(name="manual_trade_close", arguments=_parse_manual_trade_request(text))
    return None


def _extract_operation_id(text: str) -> str:
    match = _OPERATION_ID_RE.search(text)
    if match:
        return match.group(0)
    parts = [part.strip() for part in re.split(r"\s+", text.strip()) if part.strip()]
    return parts[-1] if parts else ""


def _parse_manual_trade_request(text: str) -> dict[str, object]:
    args: dict[str, object] = {"raw_text": text}
    account = _extract_account(text)
    if account:
        args["account"] = account
    return args


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
    args: dict[str, object] = {
        "symbol": labeled.get("symbol") or _extract_monitor_symbol(text),
        "set": _extract_symbol_set_values(text),
    }
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
    cleaned = re.sub(r"^(查看|增加|新增|修改|删除|移除)?监控标的", "", text.strip(), flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(symbols?|symbol)\s+(list|add|edit|remove|rm)\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return _extract_symbol(cleaned)


def _extract_symbol_set_values(text: str) -> dict[str, object]:
    out: dict[str, object] = {}
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_.]*)=([^\s,，]+)", text):
        key = match.group(1).strip()
        if key in {"symbol", "account", "record_id"}:
            continue
        out[key] = _parse_scalar(match.group(2))
    return out


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


def _looks_like_positions(compact: str, lower: str) -> bool:
    return "持仓" in compact or lower.startswith("positions") or lower.startswith("position ")


def _looks_like_income(compact: str, lower: str) -> bool:
    return "收益" in compact or "income" in lower or "pnl" in lower or "p&l" in lower


def _looks_like_runs(compact: str, lower: str) -> bool:
    return "最近运行" in compact or "运行记录" in compact or lower in {"runs", "recent runs"} or lower.startswith("runs ")


def _looks_like_logs(compact: str, lower: str) -> bool:
    return compact.startswith("日志") or lower.startswith("log ") or lower.startswith("logs ")


def _looks_like_manual_open(compact: str, lower: str) -> bool:
    return compact.startswith("记录开仓") or lower.startswith("record open") or lower.startswith("trade open")


def _looks_like_manual_close(compact: str, lower: str) -> bool:
    return compact.startswith("记录平仓") or lower.startswith("record close") or lower.startswith("trade close")


def _looks_like_symbol_list(compact: str, lower: str) -> bool:
    return compact in {"查看监控标的", "监控标的", "监控列表"} or lower in {"symbols", "symbol list", "symbols list"}


def _looks_like_symbol_add(compact: str, lower: str) -> bool:
    return compact.startswith("增加监控标的") or compact.startswith("新增监控标的") or lower.startswith("symbol add ") or lower.startswith("symbols add ")


def _looks_like_symbol_edit(compact: str, lower: str) -> bool:
    return compact.startswith("修改监控标的") or lower.startswith("symbol edit ") or lower.startswith("symbols edit ")


def _looks_like_symbol_remove(compact: str, lower: str) -> bool:
    return compact.startswith("删除监控标的") or compact.startswith("移除监控标的") or lower.startswith("symbol remove ") or lower.startswith("symbols remove ") or lower.startswith("symbols rm ")
