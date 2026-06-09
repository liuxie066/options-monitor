from __future__ import annotations

from typing import Any, cast

from src.application.assistant.commands import command_help_text
from src.application.assistant.contracts import PerceptionResult


HELP_TEXT = command_help_text()
SMALL_TALK_TEXT = "你好。我可以处理 /help 中列出的 OM 能力。发送“你能做什么”或 /help 查看完整菜单。"


def render_inbound_text(*, intent: PerceptionResult | None, tool_result: dict[str, Any] | None, error: dict[str, Any] | None = None) -> str:
    if error:
        message = str(error.get("message") or "").strip()
        hint = str(error.get("hint") or "").strip()
        if not hint:
            return message
        if "\n" in hint:
            return f"{message}\n{hint}".strip()
        return f"{message} {hint}".strip()
    if intent and intent.intent_name == "help":
        return HELP_TEXT
    if intent and intent.intent_name == "small_talk":
        return str(intent.arguments.get("response_text") or SMALL_TALK_TEXT).strip() or SMALL_TALK_TEXT
    if not tool_result:
        return "没有执行结果。"
    if not bool(tool_result.get("ok", False)):
        err_raw = tool_result.get("error")
        err = cast(dict[str, Any], err_raw) if isinstance(err_raw, dict) else {}
        message = str(err.get("message") or "查询失败")
        hint = str(err.get("hint") or "").strip()
        if "\n" in hint:
            return f"{message}\n{hint}".strip()
        return f"{message}{(' ' + hint) if hint else ''}".strip()
    name = intent.intent_name if intent else str(tool_result.get("tool_name") or "")
    data_raw = tool_result.get("data")
    data = cast(dict[str, Any], data_raw) if isinstance(data_raw, dict) else {}
    if name == "monthly_income_report":
        return _render_monthly_income(data)
    if name == "position_query":
        return _render_positions(data)
    if name == "position_exit_analysis":
        return _render_position_exit_analysis(data)
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
    if name == "symbol_config_query":
        return _render_symbol_config(data)
    return "查询完成。"


def render_pending_operations(operations: list[dict[str, Any]]) -> str:
    if not operations:
        return "当前对话没有待确认操作。"
    lines = [f"当前待确认：{len(operations)} 条"]
    for idx, operation in enumerate(operations[:10], start=1):
        operation_id = str(operation.get("operation_id") or "").strip()
        operation_type = str(operation.get("operation_type") or "").strip()
        summary = str(operation.get("summary") or operation_type or "-").strip()
        confirm, cancel = _pending_operation_commands(operation_type)
        label = _pending_operation_label(operation_type)
        lines.append(f"{idx}. {operation_id} | {label} | {summary}")
        if operation_id:
            lines.append(f"   确认：{confirm} {operation_id}")
            lines.append(f"   取消：{cancel} {operation_id}")
        expires_at = str(operation.get("expires_at") or "").strip()
        if expires_at:
            lines.append(f"   过期：{expires_at}")
    if len(operations) > 10:
        lines.append(f"... 还有 {len(operations) - 10} 条未展示。")
    return "\n".join(lines)


def _pending_operation_commands(operation_type: str) -> tuple[str, str]:
    if operation_type.startswith("symbol_"):
        return "确认监控", "取消监控"
    if operation_type.startswith("upgrade_"):
        return "确认升级", "取消升级"
    if operation_type.startswith("model_"):
        return "确认模型", "取消模型"
    return "确认记录", "取消记录"


def _pending_operation_label(operation_type: str) -> str:
    return {
        "manual_open": "交易开仓",
        "manual_close": "交易平仓",
        "symbol_add": "监控新增",
        "symbol_edit": "监控修改",
        "symbol_remove": "监控删除",
        "upgrade_now": "立即升级",
        "model_use": "模型切换",
    }.get(operation_type, operation_type or "待确认操作")


def _render_symbol_config(data: dict[str, Any]) -> str:
    symbol = _value(data.get("canonical_symbol") or data.get("symbol"))
    if data.get("missing_reason") or data.get("found") is False:
        message = str(data.get("message") or "").strip()
        if message:
            return message
        reason = _value(data.get("missing_reason"))
        return f"{symbol or '该标的'} 当前配置不可确认：{reason}。"

    path = _value(data.get("path"))
    if path and "value" in data:
        return f"{symbol} {path} = {_format_config_value(data.get('value'))}。"

    strategy = _value(data.get("strategy"))
    strategy_config = data.get("strategy_config")
    if strategy and isinstance(strategy_config, dict):
        return f"{symbol} {strategy} 当前配置：{_format_config_mapping(strategy_config)}。"

    strategies = data.get("strategies")
    if isinstance(strategies, dict) and strategies:
        lines = [f"{symbol} 当前策略配置："]
        for name, cfg in strategies.items():
            if isinstance(cfg, dict):
                lines.append(f"- {name}: {_format_config_mapping(cfg)}")
        return "\n".join(lines)

    return f"{symbol} 当前没有可展示的策略配置。"


def _format_config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _format_config_mapping(value: dict[str, Any]) -> str:
    if not value:
        return "{}"
    parts = [f"{key}={_format_config_value(item)}" for key, item in value.items() if isinstance(item, (str, int, float, bool)) or item is None]
    if not parts:
        return "{...}"
    return ", ".join(parts)


def _render_monthly_income(data: dict[str, Any]) -> str:
    detail_lines = _monthly_income_detail_lines(data)
    combined_return_summary = data.get("combined_return_summary")
    if isinstance(combined_return_summary, list) and combined_return_summary:
        combined_rows = [row for row in combined_return_summary if isinstance(row, dict) and _return_row_is_calculable(row)]
        if combined_rows:
            lines = ["收益统计完成（OM 本地账本）："]
            for idx, row in enumerate(combined_rows):
                if idx > 0:
                    lines.append("")
                lines.extend(_monthly_income_return_row_lines(row))
            return_summary = data.get("return_summary")
            account_rows = (
                [row for row in return_summary if isinstance(row, dict) and _return_row_is_calculable(row)]
                if isinstance(return_summary, list)
                else []
            )
            if account_rows:
                lines.append("")
                lines.append("分账户：")
                for row in account_rows:
                    lines.append(
                        f"- {row.get('account') or '-'}：净现金流 {_cny_with_original(row.get('net_income_cny'), _dict(row.get('net_income_by_ccy')))}"
                        f" | 现金流率 {_pct(row.get('net_return_rate'))}"
                    )
            if detail_lines:
                lines.append("")
                lines.extend(detail_lines)
            lines.append("")
            lines.append("口径：合并现金流率=sum(净现金流CNY)/sum(当前现金担保CNY)，不是账户收益率平均值。")
            return "\n".join(lines)

    return_summary = data.get("return_summary")
    if isinstance(return_summary, list) and return_summary:
        calculable_rows = [row for row in return_summary if isinstance(row, dict) and _return_row_is_calculable(row)]
        if not calculable_rows:
            return _render_monthly_income_diagnostics(data)
        lines = ["收益统计完成（OM 本地账本）："]
        long_option_recovery_notes: list[str] = []
        for idx, row in enumerate(calculable_rows):
            if not isinstance(row, dict):
                continue
            if idx > 0:
                lines.append("")
            lines.extend(_monthly_income_return_row_lines(row))
            recovery_note = _monthly_income_long_option_recovery_note(data, row)
            if recovery_note:
                long_option_recovery_notes.append(recovery_note)
        if detail_lines:
            lines.append("")
            lines.extend(detail_lines)
        lines.append("")
        lines.append("口径：现金流率=净现金流/当前现金担保，不是账户总资产收益率。")
        if long_option_recovery_notes:
            lines.append("提示：" + "；".join(_unique(long_option_recovery_notes)))
        return "\n".join(lines)

    rows = data.get("summary")
    if not isinstance(rows, list) or not rows:
        return _render_monthly_income_diagnostics(data)
    lines = ["收益统计完成（基于 OM 本地账本）："]
    for row in rows:
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
    if detail_lines:
        lines.append("")
        lines.extend(detail_lines)
    return "\n".join(lines)


def _monthly_income_detail_lines(data: dict[str, Any]) -> list[str]:
    detail_items: list[str] = []
    for row_raw in _list(data.get("realized_rows"))[:8]:
        row = _dict(row_raw)
        if not row:
            continue
        detail_items.append(
            "- 已实现 "
            f"{_monthly_income_contract_label(row)} "
            f"{_monthly_income_close_label(row.get('close_type'))} "
            f"{_contracts_text(row.get('contracts_closed'))}"
            f" | 实现 {_ccy_amount(row.get('currency'), row.get('realized_gross') if row.get('realized_gross') is not None else row.get('realized_pnl_gross'))}"
            f" | {row.get('account') or '-'}"
        )
    for row_raw in _list(data.get("cashflow_rows"))[:8]:
        row = _dict(row_raw)
        if not row:
            continue
        detail_items.append(
            "- 现金流 "
            f"{_monthly_income_contract_label(row)} "
            f"{_monthly_income_trade_action_label(row.get('trade_action'))} "
            f"{_contracts_text(row.get('contracts'))}"
            f" | 净现金流 {_ccy_amount(row.get('currency'), row.get('net_cashflow_gross'))}"
            f" | {row.get('account') or '-'}"
        )
    if not detail_items:
        return []
    realized_count = len(_list(data.get("realized_rows")))
    cashflow_count = len(_list(data.get("cashflow_rows")))
    total_count = realized_count + cashflow_count
    lines = ["组成明细：", *detail_items[:12]]
    if total_count > len(detail_items[:12]):
        lines.append(f"- 其余 {total_count - len(detail_items[:12])} 条明细已省略。")
    return lines


def _monthly_income_contract_label(row: dict[str, Any]) -> str:
    symbol = _value(row.get("symbol"))
    option_type_raw = str(row.get("option_type") or "").strip().lower()
    option_type = {"put": "Put", "call": "Call"}.get(option_type_raw, _value(row.get("option_type")))
    strike = _num(row.get("strike"))
    suffix = {"put": "P", "call": "C"}.get(option_type_raw, "")
    parts = [symbol, option_type]
    if strike != "-" and suffix:
        parts.append(f"{strike}{suffix}")
    expiration = _value(row.get("expiration_ymd") or row.get("expiration"))
    if expiration != "-":
        parts.append(f"@ {expiration}")
    return " ".join(part for part in parts if part and part != "-") or "-"


def _monthly_income_close_label(value: Any) -> str:
    key = str(value or "").strip().lower()
    return {
        "expire_auto_close": "到期作废",
        "buy_to_close": "买回平仓",
        "sell_to_close": "卖出平仓",
        "assignment": "指派平仓",
        "exercise": "行权平仓",
    }.get(key, key or "平仓")


def _monthly_income_trade_action_label(value: Any) -> str:
    key = str(value or "").strip().lower()
    return {
        "sell_open": "卖出开仓",
        "buy_open": "买入开仓",
        "buy_close": "买回平仓",
        "sell_close": "卖出平仓",
        "expire": "到期作废",
    }.get(key, key or "交易")


def _contracts_text(value: Any) -> str:
    text = _num(value)
    return f"{text}张" if text != "-" else ""


def _ccy_amount(currency: Any, amount: Any) -> str:
    currency_text = str(currency or "").strip().upper() or "-"
    value = _num(amount)
    return f"{currency_text} {value}" if value != "-" else f"{currency_text} -"


def _monthly_income_return_row_lines(row: dict[str, Any]) -> list[str]:
    annualized_days = int(row.get("annualized_basis_days") or 0)
    annualized_suffix = f"{annualized_days} 天"
    if 0 < annualized_days < 7:
        annualized_suffix += "，短周期仅参考"
    account_label = "全部账户" if row.get("account_scope") == "all" or row.get("account") == "all" else row.get("account") or "-"
    return [
        f"{account_label} {row.get('month') or '-'} 收益摘要",
        f"- 净现金流：{_cny_with_original(row.get('net_income_cny'), _dict(row.get('net_income_by_ccy')))} | 现金流率 {_pct(row.get('net_return_rate'))}",
        f"- 已实现PnL：{_cny_with_original(row.get('realized_pnl_cny'), _dict(row.get('realized_pnl_by_ccy')))} | 已实现率 {_pct(row.get('realized_return_rate'))}",
        f"- 权利金：{_cny_with_original(row.get('premium_income_cny'), _dict(row.get('premium_income_by_ccy')))} | 权利金率 {_pct(row.get('premium_return_rate'))}",
        f"- 年化：{_pct(row.get('annualized_net_return_rate'))}（按净现金流，{annualized_suffix}）",
    ]


def _monthly_income_long_option_recovery_note(data: dict[str, Any], return_row: dict[str, Any]) -> str:
    account = str(return_row.get("account") or "-")
    month = str(return_row.get("month") or "-")
    recovered_by_ccy: dict[str, float] = {}
    for summary_row_raw in _list(data.get("summary")):
        summary_row = _dict(summary_row_raw)
        if str(summary_row.get("account") or "-") != account or str(summary_row.get("month") or "-") != month:
            continue
        currency = str(summary_row.get("currency") or "").upper().strip()
        if not currency:
            continue
        realized_long = _float_or_none(summary_row.get("realized_long_pnl_gross"))
        close_proceeds = _float_or_none(summary_row.get("close_proceeds_gross"))
        if realized_long is None or close_proceeds is None or realized_long <= 0:
            continue
        recovered = close_proceeds - realized_long
        if recovered > 0:
            recovered_by_ccy[currency] = recovered_by_ccy.get(currency, 0.0) + recovered
    if not recovered_by_ccy:
        return ""
    return "净现金流包含 long option 成本回收约 " + _format_ccy_amounts(recovered_by_ccy) + "，交易盈利看已实现PnL"


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
    diag: dict[str, Any] = (
        cast(dict[str, Any], diagnostics[0])
        if isinstance(diagnostics, list) and diagnostics and isinstance(diagnostics[0], dict)
        else {}
    )
    filters_raw = data.get("filters")
    filters: dict[str, Any] = cast(dict[str, Any], filters_raw) if isinstance(filters_raw, dict) else {}
    account = diag.get("account") or filters.get("account") or "-"
    month = diag.get("month") or filters.get("month") or "-"
    return_row = _find_return_summary_row(data, account=str(account), month=str(month))
    raw_missing = diag.get("missing_fields")
    missing: list[Any] = raw_missing if isinstance(raw_missing, list) else []
    reasons = _income_missing_reasons(missing, diag=diag, return_row=return_row)
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
    if return_row:
        original_currency_lines = _original_currency_summary_lines(return_row)
        if original_currency_lines:
            lines.extend(original_currency_lines)
    warnings = data.get("report_warnings")
    if isinstance(warnings, list) and warnings:
        lines.append("诊断：" + "；".join(str(item) for item in warnings[:3]))
    return "\n".join(lines)


def _income_missing_reasons(missing_fields: list[Any], *, diag: dict[str, Any], return_row: dict[str, Any]) -> list[str]:
    missing = {str(item) for item in missing_fields}
    reasons: list[str] = []
    if "income_rows" in missing or "trade_events" in missing:
        reasons.append("本月没有匹配到已完成收益事件")
    premium_rows_count = int(diag.get("premium_rows_count") or 0) if isinstance(diag, dict) else 0
    closed_lots_count = int(diag.get("closed_lots_count") or 0) if isinstance(diag, dict) else 0
    if closed_lots_count == 0 and premium_rows_count > 0:
        reasons.append("本月暂无平仓收益")
    elif "closed_lots" in missing:
        reasons.append("账本缺少已平仓/close 数据")
    if "premium" in missing:
        reasons.append("账本缺少开仓权利金数据")
    if "cash_secured" in missing:
        reasons.append("当前持仓缺少现金担保金额")
    if "currency_conversion" in missing:
        currencies = _missing_cny_currencies(diag, return_row)
        if _dict(return_row.get("cash_secured_by_ccy")):
            reasons.append(f"现金担保原币存在，但缺少 {_ccy_pair_text(currencies)} 汇率，无法折算 CNY")
        else:
            reasons.append(f"缺少 {_ccy_pair_text(currencies)} 汇率，无法折算 CNY")
        if premium_rows_count > 0 and _dict(return_row.get("premium_income_by_ccy")):
            reasons.append("本月有开仓权利金收入，但缺汇率导致无法计算 CNY 收益率")
    if "month_range" in missing:
        reasons.append("部分事件缺少成交时间，无法归入查询月份")
    return reasons


def _find_return_summary_row(data: dict[str, Any], *, account: str, month: str) -> dict[str, Any]:
    rows = data.get("return_summary")
    if not isinstance(rows, list):
        return {}
    for row_raw in rows:
        row = _dict(row_raw)
        if str(row.get("account") or "-") == account and str(row.get("month") or "-") == month:
            return row
    return _dict(rows[0]) if rows else {}


def _missing_cny_currencies(diag: dict[str, Any], return_row: dict[str, Any]) -> list[str]:
    raw = diag.get("missing_cny_currencies") if isinstance(diag, dict) else None
    if isinstance(raw, list) and raw:
        return sorted({str(item).upper() for item in raw if str(item).strip()})
    currencies: set[str] = set()
    for key in ("cash_secured_by_ccy", "net_income_by_ccy", "premium_income_by_ccy", "realized_pnl_by_ccy"):
        values = _dict(return_row.get(key))
        currencies.update(str(currency).upper() for currency in values if str(currency).strip())
    return sorted(currencies)


def _ccy_pair_text(currencies: list[str]) -> str:
    if not currencies:
        return "币种到 CNY"
    return "/".join(currencies) + " 到 CNY"


def _original_currency_summary_lines(return_row: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    net = _dict(return_row.get("net_income_by_ccy"))
    premium = _dict(return_row.get("premium_income_by_ccy"))
    if net:
        lines.append("净现金流：" + _format_ccy_amounts(net))
    if premium:
        lines.append("权利金：" + _format_ccy_amounts(premium))
    cash = _dict(return_row.get("cash_secured_by_ccy"))
    premium_rates = _dict(return_row.get("premium_return_rate_by_ccy"))
    if not premium_rates and premium and cash:
        premium_rates = _rate_by_ccy_for_render(premium, cash)
    if premium_rates:
        lines.append("原币权利金收益率：" + _format_ccy_rates(premium_rates))
    return lines


def _rate_by_ccy_for_render(numerator_by_ccy: dict[str, Any], denominator_by_ccy: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for currency, numerator in numerator_by_ccy.items():
        try:
            denominator = float(denominator_by_ccy.get(currency) or 0.0)
            if denominator > 0:
                out[str(currency).upper()] = float(numerator or 0.0) / denominator
        except Exception:
            continue
    return out


def _format_ccy_amounts(values: dict[str, Any]) -> str:
    parts: list[str] = []
    for currency, amount in sorted(values.items()):
        try:
            parts.append(f"{str(currency).upper()} {float(amount):,.0f}")
        except Exception:
            continue
    return " + ".join(parts) if parts else "-"


def _format_ccy_rates(values: dict[str, Any]) -> str:
    parts: list[str] = []
    for currency, value in sorted(values.items()):
        pct = _pct(value)
        if pct != "-":
            parts.append(f"{str(currency).upper()} {pct}")
    return "，".join(parts) if parts else "-"


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _cny_with_original(cny_value: Any, by_ccy: dict[str, Any]) -> str:
    cny_text = _cny(cny_value)
    original = _format_ccy_amounts(by_ccy)
    if original == "-":
        return cny_text
    return f"{cny_text}（{original}）"


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
    query = _dict(filters.get("query"))
    account = _value(query.get("account") if query else filters.get("account"))
    account_label = "全部账户" if account == "-" else account
    status = _value(query.get("status") if query else filters.get("status") or "open")
    scope = _position_scope_text(account_label=account_label, status=status, query=query, filters=filters)
    if not rows:
        return f"{scope}：0 条。\n数据源：OM 本地 SQLite position_lots"

    lines = [f"{scope}：{len(rows)} 条"]
    for row_raw in rows:
        row = _dict(row_raw)
        lines.append(
            "- "
            f"{_value(row.get('symbol'))} "
            f"{_value(row.get('side'))} {_value(row.get('option_type'))} "
            f"{_num(row.get('strike'))} "
            f"exp {(_value(row.get('expiration_ymd') or row.get('expiration')))} "
            f"open {_num(row.get('contracts_open') if row.get('contracts_open') is not None else row.get('contracts'))}"
        )
    bootstrap = _dict(data.get("bootstrap"))
    bootstrap_status = str(bootstrap.get("status") or "").strip()
    if bootstrap_status.startswith("degraded"):
        lines.append("账本提示：" + _value(bootstrap.get("message") or bootstrap_status))
    lines.append("数据源：OM 本地 SQLite position_lots")
    return "\n".join(lines)


def _render_position_exit_analysis(data: dict[str, Any]) -> str:
    rows = _list(data.get("rows"))
    query = _dict(data.get("query"))
    source = _dict(data.get("source"))
    scope = _position_exit_scope_text(query)
    source_text = _close_advice_source_text(source)
    if not rows:
        matched = int(data.get("matched_count") or 0)
        if matched <= 0:
            return f"平仓建议分析：没有找到匹配的 open 持仓。\n范围：{scope}\n数据源：{source_text}"
        return f"平仓建议分析：匹配 {matched} 条，但本次没有返回明细。\n范围：{scope}\n数据源：{source_text}"

    lines = [
        f"平仓建议分析（基于最近一次 Close Advice 报告）：{len(rows)} 条",
        f"范围：{scope}",
    ]
    for row_raw in rows:
        row = _dict(row_raw)
        lines.append(
            "- "
            f"{_value(row.get('account'))} "
            f"{_value(row.get('symbol'))} "
            f"{_value(row.get('side'))} {_value(row.get('option_type'))} "
            f"{_value(row.get('expiration') or row.get('expiration_ymd'))} "
            f"{_num(row.get('strike'))}"
        )
        conclusion = _close_advice_conclusion(row)
        if conclusion != "-":
            lines.append(f"  结论：{conclusion}")
        optional_action = _close_action_label(row.get("optional_combo_action"))
        if optional_action != "-":
            lines.append(f"  可选：{optional_action}")
        status = _value(row.get("evaluation_status") or row.get("quote_status"))
        if status != "-":
            lines.append(f"  状态：{status}")
        reason = _close_advice_reason(row)
        if reason != "-":
            lines.append(f"  原因：{reason}")
        metrics = _close_advice_metric_text(row)
        if metrics:
            lines.append(f"  指标：{metrics}")
    lines.append(f"数据源：{source_text}")
    return "\n".join(lines)


def _position_exit_scope_text(query: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("account", "symbol", "side", "option_type"):
        value = _value(query.get(key))
        if value != "-":
            parts.append(value)
    if query.get("strike") is not None:
        parts.append(f"strike {_num(query.get('strike'))}")
    expiration = _dict(query.get("expiration"))
    expiration_text = _position_expiration_scope(expiration, filters={})
    if expiration_text:
        parts.append(expiration_text)
    return " · ".join(parts) if parts else "全部 open 期权持仓"


def _close_advice_source_text(source: dict[str, Any]) -> str:
    run_id = _value(source.get("run_id"))
    paths = _list(source.get("paths"))
    if run_id != "-":
        return f"run {run_id}"
    if paths:
        return _value(paths[0])
    return "-"


def _close_advice_conclusion(row: dict[str, Any]) -> str:
    action = _close_action_label(row.get("close_action"))
    tier_label = _value(row.get("tier_label"))
    if action != "-":
        return action if tier_label == "-" or tier_label == action else f"{action}（{tier_label}）"
    for key in ("tier_label", "tier", "exit_state"):
        value = _value(row.get(key))
        if value != "-":
            return value
    return "-"


def _close_action_label(value: Any) -> str:
    action = str(value or "").strip().lower()
    if not action:
        return "-"
    mapping = {
        "close_put_keep_call": "买回 Put，保留收益增强 Call",
        "hold_put_keep_call": "继续持有 Put，保留收益增强 Call",
        "sell_call_take_profit": "卖出 Call 止盈",
        "hold_call": "继续持有 Call",
        "hold_call_as_convexity": "继续持有 Call 凸性腿",
        "sell_call_salvage": "卖出 Call 回收残值",
        "hold_to_expiry_or_expire": "保留至到期或允许归零",
        "close_both_optional": "可选组合止盈",
        "close": "平仓",
        "hold": "持有观察",
        "not_evaluable": "无法评估",
    }
    return mapping.get(action, action)


def _close_advice_reason(row: dict[str, Any]) -> str:
    for key in ("reason", "short_vol_reason", "optimizer_reason"):
        value = _value(row.get(key))
        if value != "-":
            return value
    return "-"


def _close_advice_metric_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    realized = row.get("realized_if_close")
    if realized is not None:
        parts.append(f"平仓收益 {_num(realized)}")
    put_realized = row.get("put_leg_realized_if_close")
    if put_realized is not None:
        parts.append(f"Put腿收益 {_num(put_realized)}")
    combo_locked = row.get("combo_net_locked_if_close_put_keep_call")
    if combo_locked is not None:
        parts.append(f"组合锁定净收益 {_num(combo_locked)}")
    combo_both = row.get("combo_net_if_close_both")
    if combo_both is not None:
        parts.append(f"组合全平净收益 {_num(combo_both)}")
    call_value = row.get("long_call_current_value")
    if call_value is not None:
        parts.append(f"Call现值 {_num(call_value)}")
    call_cost = row.get("long_call_cost_basis")
    if call_cost is not None:
        parts.append(f"Call成本 {_num(call_cost)}")
    call_ratio = row.get("long_call_value_ratio")
    if call_ratio is not None:
        parts.append(f"Call现值/成本 {_num(call_ratio)}")
    capture = row.get("capture_ratio")
    if capture is not None:
        parts.append(f"收益捕获 {_pct(capture)}")
    remaining = row.get("remaining_annualized_return")
    if remaining is not None:
        parts.append(f"剩余年化 {_pct(remaining)}")
    iv_rv = row.get("iv_rv_ratio")
    if iv_rv is not None:
        parts.append(f"IV/RV {_num(iv_rv)}")
    delta = row.get("abs_delta") if row.get("abs_delta") is not None else row.get("delta")
    if delta is not None:
        parts.append(f"delta {_num(delta)}")
    event_status = _value(row.get("event_source_status"))
    if event_status != "-":
        parts.append(f"事件源 {event_status}")
    path_status = _value(row.get("path_stress_status"))
    if path_status != "-":
        parts.append(f"路径压力 {path_status}")
    return "，".join(parts)


def _position_scope_text(*, account_label: str, status: str, query: dict[str, Any], filters: dict[str, Any]) -> str:
    parts = [account_label, status]
    symbol = _value(query.get("symbol") if query else filters.get("symbol"))
    option_type = _value(query.get("option_type") if query else filters.get("option_type"))
    side = _value(query.get("side") if query else filters.get("side"))
    strike = query.get("strike") if query else filters.get("strike")
    expiration = _dict(query.get("expiration") if query else filters.get("expiration"))
    if symbol != "-":
        parts.append(symbol)
    if side != "-":
        parts.append(side)
    if option_type != "-":
        parts.append(option_type)
    if strike is not None:
        parts.append(f"strike {_num(strike)}")
    expiration_text = _position_expiration_scope(expiration, filters=filters)
    if expiration_text:
        parts.append(expiration_text)
    parts.append("期权持仓")
    return " · ".join(parts)


def _position_expiration_scope(expiration: dict[str, Any], *, filters: dict[str, Any]) -> str:
    if expiration.get("exact"):
        return f"{_value(expiration.get('exact'))} 到期"
    if expiration.get("month"):
        return f"{_value(expiration.get('month'))} 到期"
    if expiration.get("before"):
        return f"{_value(expiration.get('before'))} 前到期"
    if expiration.get("after"):
        return f"{_value(expiration.get('after'))} 后到期"
    within_days = expiration.get("within_days")
    if within_days is None:
        within_days = filters.get("expiration_within_days")
    if within_days is not None:
        return f"{_num(within_days)} 天内到期"
    return ""


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
    warnings = tool_result.get("warnings")
    has_warnings = bool(warnings) if isinstance(warnings, list) else bool(summary.get("warning_count"))
    if tool_result.get("ok") is False or summary.get("ok") is False or has_warnings:
        status = "degraded"
    elif tool_result.get("ok") is True or summary.get("ok") is True:
        status = "ok"
    else:
        status = _value(summary.get("latest_status") or "unknown")
    lines = [f"OM 状态：{status}"]

    latest_status = summary.get("latest_status")
    if latest_status:
        lines.append(f"最新状态：{latest_status}")

    latest_run = _dict(data.get("latest_run"))
    if latest_run:
        run_id = _run_id_from_path(latest_run.get("path"))
        tick_metrics = _json_file_payload(_dict(_dict(latest_run.get("state")).get("tick_metrics")))
        if tick_metrics:
            lines.append(
                "最新运行："
                f"{_value(run_id)} "
                f"scan={_yes_no(tick_metrics.get('ran_scan'))} "
                f"notify={_runtime_notify_text(tick_metrics)}"
            )

    shared_last_run = _json_file_payload(_dict(_dict(data.get("shared")).get("last_run")))
    if shared_last_run and not any(line.startswith("最新运行：") for line in lines):
        lines.append(
            "最新通知："
            f"scan={_yes_no(_shared_last_run_ran_scan(shared_last_run))} "
            f"notify={_runtime_notify_text(shared_last_run)}"
        )
    elif latest_run and not any(line.startswith("最新运行：") for line in lines):
        lines.append(f"最新运行：{_value(_run_id_from_path(latest_run.get('path')))} scan=- notify=-")

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

    if summary.get("service_upgrade_runtime_failed"):
        lines.extend(_runtime_service_upgrade_lines(data))

    trade_intake = _dict(data.get("trade_intake"))
    intake_summary = _dict(trade_intake.get("summary"))
    if intake_summary:
        lines.append(f"交易监听：{_value(intake_summary.get('listener_status'))}")

    auto_close_lines = _runtime_auto_close_lines(data)
    lines.extend(auto_close_lines[:2])

    if isinstance(warnings, list) and warnings:
        lines.append("异常：" + "；".join(str(item) for item in warnings[:3]))
    elif summary.get("warning_count"):
        lines.append(f"异常：{summary.get('warning_count')} 个 warning，详情用 健康检查 或 最近运行。")
    else:
        lines.append("异常：无")
    return "\n".join(lines)


def _runtime_service_upgrade_lines(data: dict[str, Any]) -> list[str]:
    summary = _dict(data.get("summary"))
    service_upgrade = _dict(data.get("service_upgrade"))
    evaluation = _dict(service_upgrade.get("evaluation"))
    status = summary.get("service_upgrade_status") or evaluation.get("status")
    target = summary.get("service_upgrade_target_version") or evaluation.get("target_version")
    current = summary.get("service_upgrade_current_version") or evaluation.get("current_version")
    reason = summary.get("service_upgrade_reason") or evaluation.get("reason")
    lines = [f"升级状态：{_value(status)} target={_value(target)} current={_value(current)} reason={_value(reason)}"]
    failed_services = summary.get("service_upgrade_failed_services") or evaluation.get("failed_services")
    failed = [str(item).strip() for item in _list(failed_services) if str(item).strip()]
    if failed:
        lines.append("失败服务：" + ", ".join(failed))
    remediation = summary.get("service_upgrade_remediation") or evaluation.get("remediation")
    hints = [str(item).strip() for item in _list(remediation) if str(item).strip()]
    if hints:
        lines.append("修复提示：" + "；".join(hints[:2]))
    return lines


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
        errors = maintenance.get("errors")
        has_errors = isinstance(errors, list) and bool(errors)
        mode = str(maintenance.get("mode") or "").strip().lower()
        if mode in {"error", "failed"} or has_errors:
            applied = maintenance.get("applied_closed")
            reason = maintenance.get("reason") or (errors[0] if has_errors else None)
            parts = ["failed"]
            if receipt.get("status"):
                parts.append(f"receipt={_value(receipt.get('status'))}")
            if applied is not None:
                parts.append(f"closed={applied}")
            if reason:
                parts.append(f"reason={_value(reason)}")
            out.append(f"auto-close {account}：" + "，".join(parts))
            continue
        status = receipt.get("status") or maintenance.get("mode")
        if status:
            applied = maintenance.get("applied_closed")
            suffix = f"，closed={applied}" if applied is not None else ""
            out.append(f"auto-close {account}：{_value(status)}{suffix}")
    return out


def _runtime_notify_text(payload: dict[str, Any]) -> str:
    notify = _dict(payload.get("notify_summary"))
    if not notify and not payload:
        return "-"
    confirmed = _as_int(notify.get("send_confirmed_count") or payload.get("send_confirmed_count"))
    attempted = _as_int(
        notify.get("send_attempted_count")
        or payload.get("send_attempted_count")
        or notify.get("account_messages_count")
        or payload.get("account_messages_count")
    )
    if confirmed == 0 and attempted == 0 and not notify:
        return "-"
    return f"{confirmed}/{attempted}"


def _shared_last_run_ran_scan(payload: dict[str, Any]) -> bool | None:
    if isinstance(payload.get("ran_scan"), bool):
        return bool(payload.get("ran_scan"))
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    saw_false = False
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("ran_scan") is True:
            return True
        if item.get("ran_scan") is False:
            saw_false = True
    return False if saw_false else None


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


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        out.append(value)
        seen.add(value)
    return out


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "-"


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


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
