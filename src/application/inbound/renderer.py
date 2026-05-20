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
        return _render_runtime_status(data, tool_result)
    if name == "healthcheck":
        return _render_healthcheck(data, tool_result)
    if name == "config_validate":
        return _render_config_validate(data, tool_result)
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
    filters = _dict(data.get("filters"))
    account = _value(filters.get("account"))
    status = _value(filters.get("status") or "open")
    if not rows:
        return f"{account} 当前没有 {status} 期权持仓。\n数据源：OM 本地 SQLite position_lots"

    lines = [f"{account} 当前 {status} 期权持仓：{len(rows)} 条"]
    for row_raw in rows[:10]:
        row = _dict(row_raw)
        lines.append(
            "- "
            f"{_value(row.get('symbol'))} "
            f"{_value(row.get('side'))} {_value(row.get('option_type'))} "
            f"{_num(row.get('strike'))} "
            f"exp {(_value(row.get('expiration_ymd') or row.get('expiration')))} "
            f"open {_num(row.get('contracts_open') if row.get('contracts_open') is not None else row.get('contracts'))}"
        )
    if len(rows) > 10:
        lines.append(f"... 还有 {len(rows) - 10} 条未展示。")
    bootstrap = _dict(data.get("bootstrap"))
    bootstrap_status = str(bootstrap.get("status") or "").strip()
    if bootstrap_status.startswith("degraded"):
        lines.append("账本提示：" + _value(bootstrap.get("message") or bootstrap_status))
    lines.append("数据源：OM 本地 SQLite position_lots")
    return "\n".join(lines)


def _render_runs(data: dict[str, Any]) -> str:
    selected = _dict(data.get("selected_run"))
    if selected:
        scheduler = _dict(selected.get("scheduler"))
        lines = [
            f"运行 {selected.get('run_id') or '-'}：{_value(selected.get('status'))}",
            f"时间：{_value(selected.get('mtime_utc'))}",
            f"扫描：{_yes_no(selected.get('ran_scan'))}，通知：{_yes_no(selected.get('sent'))}",
            f"账户：{_csv(selected.get('accounts'))}",
            f"原因：{_value(selected.get('reason'))}",
        ]
        if scheduler:
            lines.append(
                "调度："
                f"scan={_yes_no(scheduler.get('should_run_scan'))} "
                f"notify={_yes_no(scheduler.get('should_notify'))} "
                f"{_value(scheduler.get('reason'))}"
            )
        return "\n".join(lines)

    runs = data.get("runs")
    if not isinstance(runs, list):
        return "最近运行查询完成。"
    summary = _dict(data.get("summary"))
    total = summary.get("total_count")
    returned = summary.get("returned_count")
    if not runs:
        return "最近运行：没有找到运行记录。"
    lines = [f"最近运行：{_value(returned if returned is not None else len(runs))}/{_value(total if total is not None else len(runs))} 条"]
    for row_raw in runs[:8]:
        row = _dict(row_raw)
        lines.append(
            "- "
            f"{_value(row.get('run_id'))} "
            f"{_value(row.get('status'))} "
            f"{_value(row.get('mtime_utc'))} "
            f"scan={_yes_no(row.get('ran_scan'))} "
            f"sent={_yes_no(row.get('sent'))} "
            f"accounts={_csv(row.get('accounts'))} "
            f"reason={_value(row.get('reason'))}"
        )
    if len(runs) > 8:
        lines.append(f"... 还有 {len(runs) - 8} 条未展示。")
    return "\n".join(lines)


def _render_logs(data: dict[str, Any]) -> str:
    files = data.get("files")
    if not isinstance(files, list):
        return "日志查询完成。"
    summary = _dict(data.get("summary"))
    run = _dict(data.get("selected_run"))
    header = (
        f"日志查询：{int(summary.get('existing_file_count') or 0)}/{len(files)} 个文件"
        f"，kind={_value(summary.get('kind'))}，lines={_value(summary.get('lines'))}"
    )
    lines = [header]
    if run:
        lines.append(f"run：{_value(run.get('run_id'))}")
    if not files:
        lines.append("没有找到日志文件。")
        return "\n".join(lines)
    for file_raw in files[:3]:
        entry = _dict(file_raw)
        lines.append(
            "- "
            f"{_value(entry.get('path_display') or entry.get('path'))} "
            f"exists={_yes_no(entry.get('exists'))} "
            f"tail={_value(entry.get('tail_line_count'))}"
        )
        error = str(entry.get("error") or "").strip()
        if error:
            lines.append(f"  error: {error}")
        tail = entry.get("tail")
        if isinstance(tail, list) and tail:
            for item in tail[-3:]:
                lines.append("  " + str(item)[:220])
    if len(files) > 3:
        lines.append(f"... 还有 {len(files) - 3} 个文件未展示。")
    return "\n".join(lines)


def _render_runtime_status(data: dict[str, Any], tool_result: dict[str, Any]) -> str:
    summary = _dict(data.get("summary"))
    status = "ok" if summary.get("ok") is True else "degraded" if summary.get("ok") is False else _value(summary.get("latest_status") or "unknown")
    lines = [f"OM 状态：{status}"]

    latest_status = summary.get("latest_status")
    if latest_status:
        lines.append(f"最新状态：{latest_status}")

    latest_run = _dict(data.get("latest_run"))
    if latest_run:
        run_id = _run_id_from_path(latest_run.get("path"))
        tick_metrics = _json_file_payload(_dict(_dict(latest_run.get("state")).get("tick_metrics")))
        notify = _dict(tick_metrics.get("notify_summary"))
        notify_text = "-"
        if notify:
            notify_text = f"{int(notify.get('send_confirmed_count') or 0)}/{int(notify.get('send_attempted_count') or notify.get('account_messages_count') or 0)}"
        lines.append(
            "最新运行："
            f"{_value(run_id)} "
            f"scan={_yes_no(tick_metrics.get('ran_scan') if tick_metrics else None)} "
            f"notify={notify_text}"
        )

    latest_scanned = _dict(data.get("latest_scanned_run"))
    if latest_scanned and latest_scanned is not latest_run:
        lines.append(f"最近扫描：{_value(_run_id_from_path(latest_scanned.get('path')))}")

    ledger_status = summary.get("ledger_status")
    if ledger_status is not None:
        lines.append(
            "账本："
            f"{_value(ledger_status)} "
            f"lots={_value(summary.get('ledger_position_lot_count'))} "
            f"events={_value(summary.get('ledger_trade_event_count'))}"
        )

    projection_ok = summary.get("projection_verify_ok")
    if projection_ok is not None:
        lines.append(f"Projection：{_yes_no(projection_ok)} mode={_value(summary.get('projection_verify_mode'))}")

    trade_intake = _dict(data.get("trade_intake"))
    intake_summary = _dict(trade_intake.get("summary"))
    if intake_summary:
        lines.append(f"交易监听：{_value(intake_summary.get('listener_status'))}")

    auto_close_lines = _runtime_auto_close_lines(data)
    lines.extend(auto_close_lines[:2])

    warnings = tool_result.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.append("异常：" + "；".join(str(item) for item in warnings[:3]))
    elif summary.get("warning_count"):
        lines.append(f"异常：{summary.get('warning_count')} 个 warning，详情用 健康检查 或 最近运行。")
    else:
        lines.append("异常：无")
    return "\n".join(lines)


def _render_healthcheck(data: dict[str, Any], tool_result: dict[str, Any]) -> str:
    summary = _dict(data.get("summary"))
    ok = summary.get("ok")
    status = "ok" if ok is True else "degraded" if ok is False else ("ok" if tool_result.get("ok") else "error")
    critical_count = int(summary.get("critical_count") or 0)
    warning_count = int(summary.get("warning_count") or 0)
    lines = [f"健康检查：{status}", f"失败：{critical_count}，警告：{warning_count}"]
    checks = [_dict(item) for item in _list(data.get("checks"))]
    issues = [item for item in checks if str(item.get("status") or "").lower() in {"error", "warn"}]
    for item in issues[:5]:
        lines.append(f"- {_value(item.get('status'))} {_value(item.get('name'))}: {_value(item.get('message'))}")
    if not issues:
        lines.append("关键检查通过。")
    warnings = tool_result.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.append("提示：" + "；".join(str(item) for item in warnings[:3]))
    return "\n".join(lines)


def _render_config_validate(data: dict[str, Any], tool_result: dict[str, Any]) -> str:
    warnings = data.get("warnings")
    warning_items = warnings if isinstance(warnings, list) else []
    ok = bool(tool_result.get("ok", False)) and not warning_items
    lines = [
        f"配置检查：{'通过' if ok else '有警告'}",
        f"config：{_value(data.get('config_path') or data.get('config_key'))}",
        f"账户：{_csv(data.get('accounts'))}（{_value(data.get('account_count'))} 个）",
        f"监控标的：{_value(data.get('symbol_count'))} 个",
    ]
    if warning_items:
        lines.append("警告：" + "；".join(str(item) for item in warning_items[:5]))
    return "\n".join(lines)


def _runtime_auto_close_lines(data: dict[str, Any]) -> list[str]:
    latest_run = _dict(data.get("latest_run"))
    accounts = _dict(latest_run.get("accounts"))
    out: list[str] = []
    for account, payload in accounts.items():
        info = _dict(payload)
        receipt = _dict(info.get("auto_close_receipt"))
        maintenance = _json_file_payload(_dict(info.get("expired_position_maintenance")))
        status = receipt.get("status") or maintenance.get("mode")
        if status:
            applied = maintenance.get("applied_closed")
            suffix = f"，closed={applied}" if applied is not None else ""
            out.append(f"auto-close {account}：{_value(status)}{suffix}")
    return out


def _json_file_payload(file_info: dict[str, Any]) -> dict[str, Any]:
    return _dict(file_info.get("json"))


def _run_id_from_path(path: Any) -> str | None:
    text = str(path or "").strip()
    if not text:
        return None
    return text.rstrip("/").split("/")[-1] or text


def _dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _value(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"


def _csv(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value) or "-"
    return _value(value)


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "-"


def _num(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except Exception:
        return _value(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")
