from __future__ import annotations

import re
from datetime import date
from typing import Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.inbound.contracts import InboundIntent


_ACCOUNT_RE = re.compile(r"(?<![a-z0-9_])(lx|sy)(?![a-z0-9_])", re.IGNORECASE)
_MONTH_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])(?!\d)")
_INT_RE = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")


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


def _looks_like_positions(compact: str, lower: str) -> bool:
    return "持仓" in compact or lower.startswith("positions") or lower.startswith("position ")


def _looks_like_income(compact: str, lower: str) -> bool:
    return "收益" in compact or "income" in lower or "pnl" in lower or "p&l" in lower


def _looks_like_runs(compact: str, lower: str) -> bool:
    return "最近运行" in compact or "运行记录" in compact or lower in {"runs", "recent runs"} or lower.startswith("runs ")


def _looks_like_logs(compact: str, lower: str) -> bool:
    return compact.startswith("日志") or lower.startswith("log ") or lower.startswith("logs ")
