from __future__ import annotations

from typing import Any, cast

from src.application.inbound.contracts import InboundIntent


HELP_TEXT = (
    "可用只读命令：状态、健康检查、持仓 sy、收益 sy、收益 sy 2026-05、最近运行、日志 <run_id>。"
)


def render_inbound_text(*, intent: InboundIntent | None, tool_result: dict[str, Any] | None, error: dict[str, Any] | None = None) -> str:
    if error:
        hint = str(error.get("hint") or "").strip()
        return f"{error.get('message')}{(' ' + hint) if hint else ''}".strip()
    if intent and intent.name == "help":
        return HELP_TEXT
    if not tool_result:
        return "没有执行结果。"
    if not bool(tool_result.get("ok", False)):
        err_raw = tool_result.get("error")
        err = cast(dict[str, Any], err_raw) if isinstance(err_raw, dict) else {}
        message = str(err.get("message") or "查询失败")
        hint = str(err.get("hint") or "").strip()
        return f"{message}{(' ' + hint) if hint else ''}".strip()
    name = intent.name if intent else str(tool_result.get("tool_name") or "")
    data_raw = tool_result.get("data")
    data = cast(dict[str, Any], data_raw) if isinstance(data_raw, dict) else {}
    if name == "monthly_income_report":
        return _render_monthly_income(data)
    if name == "option_positions_open":
        return _render_positions(data)
    if name == "runtime_runs":
        return _render_runs(data)
    if name == "runtime_logs":
        return _render_logs(data)
    if name == "runtime_status":
        return "状态查询完成。"
    if name == "healthcheck":
        summary_raw = data.get("summary")
        summary = cast(dict[str, Any], summary_raw) if isinstance(summary_raw, dict) else {}
        status = summary.get("status") or ("ok" if tool_result.get("ok") else "error")
        return f"健康检查完成：{status}。"
    if name == "config_validate":
        return "配置检查完成。"
    return "查询完成。"


def _render_monthly_income(data: dict[str, Any]) -> str:
    rows = data.get("summary")
    if not isinstance(rows, list) or not rows:
        return "收益统计完成：没有匹配的月度收益记录。"
    lines = ["收益统计完成（基于 OM 本地账本）："]
    for row in rows[:6]:
        if not isinstance(row, dict):
            continue
        lines.append(
            "- "
            f"{row.get('month') or '-'} "
            f"{row.get('account') or '-'} "
            f"{row.get('currency') or '-'} "
            f"cashflow={row.get('net_cashflow_gross', 0)} "
            f"realized={row.get('realized_pnl_gross', 0)} "
            f"open_basis={row.get('open_basis_lifecycle_pnl_gross', 0)}"
        )
    return "\n".join(lines)


def _render_positions(data: dict[str, Any]) -> str:
    rows = data.get("rows")
    if not isinstance(rows, list):
        rows = data.get("positions")
    if not isinstance(rows, list):
        return "持仓查询完成。"
    return f"持仓查询完成：{len(rows)} 条记录。"


def _render_runs(data: dict[str, Any]) -> str:
    runs = data.get("runs")
    if not isinstance(runs, list):
        return "最近运行查询完成。"
    return f"最近运行查询完成：{len(runs)} 条记录。"


def _render_logs(data: dict[str, Any]) -> str:
    files = data.get("files")
    if isinstance(files, list):
        return f"日志查询完成：{len(files)} 个文件。"
    return "日志查询完成。"
