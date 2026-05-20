from __future__ import annotations

from typing import Any, cast

from src.application.inbound.contracts import InboundIntent


HELP_TEXT = (
    "可用只读命令：状态、健康检查、持仓 sy、收益 sy、收益 sy 2026-05、最近运行、日志 <run_id>、查看监控标的。\n"
    "管理员写操作：记录开仓/记录平仓、增加/修改/删除监控标的。写操作会先返回预览，确认请回复：确认记录 <operation_id> 或 确认监控 <operation_id>。"
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
    return_summary = data.get("return_summary")
    if isinstance(return_summary, list) and return_summary:
        calculable_rows = [row for row in return_summary if isinstance(row, dict) and _return_row_is_calculable(row)]
        if not calculable_rows:
            return _render_monthly_income_diagnostics(data)
        lines = ["收益统计完成（基于 OM 本地账本）："]
        for row in calculable_rows[:4]:
            if not isinstance(row, dict):
                continue
            lines.extend(
                [
                    f"{row.get('account') or '-'} {row.get('month') or '-'} 收益摘要",
                    f"净收益率：{_pct(row.get('net_return_rate'))}",
                    f"净收入：{_cny(row.get('net_income_cny'))}",
                    f"现金担保：{_cny(row.get('cash_secured_cny'))}",
                    f"按 {row.get('annualized_basis_days') or 0} 天折年化：{_pct(row.get('annualized_net_return_rate'))}",
                    f"权利金毛收益率：{_pct(row.get('premium_return_rate'))}",
                ]
            )
        return "\n".join(lines)

    rows = data.get("summary")
    if not isinstance(rows, list) or not rows:
        return _render_monthly_income_diagnostics(data)
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


def _return_row_is_calculable(row: dict[str, Any]) -> bool:
    try:
        cash = row.get("cash_secured_cny")
        if cash is None or float(cash) <= 0:
            return False
    except Exception:
        return False
    for key in (
        "net_return_rate",
        "premium_return_rate",
        "realized_return_rate",
        "net_income_cny",
        "premium_income_cny",
        "realized_pnl_cny",
    ):
        if row.get(key) is not None:
            return True
    return False


def _render_monthly_income_diagnostics(data: dict[str, Any]) -> str:
    diagnostics = data.get("diagnostics")
    diag = diagnostics[0] if isinstance(diagnostics, list) and diagnostics and isinstance(diagnostics[0], dict) else {}
    filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
    account = diag.get("account") or filters.get("account") or "-"
    month = diag.get("month") or filters.get("month") or "-"
    missing = diag.get("missing_fields") if isinstance(diag.get("missing_fields"), list) else []
    reasons = _income_missing_reasons(missing)
    if not reasons:
        reasons = ["没有可计算收益数据。"]
    lines = [
        f"{account} {month} 暂无可计算收益。",
        "原因：" + "；".join(reasons),
    ]
    if diag:
        lines.append(
            "匹配事件："
            f"{int(diag.get('matched_trade_events_count') or 0)}，"
            f"持仓 lot：{int(diag.get('matched_lots_count') or 0)}，"
            f"已平仓 lot：{int(diag.get('closed_lots_count') or 0)}，"
            f"权利金行：{int(diag.get('premium_rows_count') or 0)}。"
        )
        if missing:
            lines.append("缺失项：" + "、".join(str(item) for item in missing[:8]))
    warnings = data.get("report_warnings")
    if isinstance(warnings, list) and warnings:
        lines.append("诊断：" + "；".join(str(item) for item in warnings[:3]))
    return "\n".join(lines)


def _income_missing_reasons(missing_fields: list[Any]) -> list[str]:
    missing = {str(item) for item in missing_fields}
    reasons: list[str] = []
    if "income_rows" in missing or "trade_events" in missing:
        reasons.append("本月没有匹配到已完成收益事件")
    if "closed_lots" in missing:
        reasons.append("账本缺少已平仓/close 数据")
    if "premium" in missing:
        reasons.append("账本缺少开仓权利金数据")
    if "cash_secured" in missing:
        reasons.append("当前持仓缺少现金担保金额")
    if "currency_conversion" in missing:
        reasons.append("缺少币种换算汇率，无法折算 CNY")
    if "month_range" in missing:
        reasons.append("部分事件缺少成交时间，无法归入查询月份")
    return reasons


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return "-"


def _cny(value: Any) -> str:
    if value is None:
        return "CNY -"
    try:
        return f"CNY {float(value):,.0f}"
    except Exception:
        return "CNY -"


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
