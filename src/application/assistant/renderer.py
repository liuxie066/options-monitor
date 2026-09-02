from __future__ import annotations

from typing import Any, Callable, cast

from src.application.assistant.capability_catalog import command_help_text
from src.application.assistant.contracts import ControlCommand
from src.application.payload_helpers import as_dict as _dict


HELP_TEXT = command_help_text()
SMALL_TALK_TEXT = "你好。我可以处理 /help 中列出的 OM 能力。发送“你能做什么”或 /help 查看完整菜单。"


def render_canonical_tool_result(*, renderer_key: str, data: dict[str, Any], tool_result: dict[str, Any]) -> str:
    key = str(renderer_key or "").strip()
    renderer = _CANONICAL_RENDERERS.get(key)
    return renderer(data, tool_result) if renderer is not None else ""


def render_inbound_text(*, intent: ControlCommand | None, tool_result: dict[str, Any] | None, error: dict[str, Any] | None = None) -> str:
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
    renderer_key = {
        "option_performance_report": "option_performance",
        "position_query": "position_rows",
        "assigned_stock_position_query": "assigned_stock_lifecycle",
        "position_exit_analysis": "position_exit_analysis",
        "runtime_runs": "runtime_runs",
        "runtime_logs": "runtime_logs",
        "runtime_status": "runtime_status",
        "healthcheck": "healthcheck",
        "config_validate": "config_validate",
        "symbol_config_query": "symbol_config",
        "symbol_resolve": "symbol_resolve",
        "candidate_filter_explain": "candidate_filter_explain",
        "cash_headroom_query": "cash_headroom",
    }.get(name)
    if renderer_key:
        rendered = render_canonical_tool_result(renderer_key=renderer_key, data=data, tool_result=tool_result)
        if rendered:
            return rendered
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
        return "/confirm symbol", "/cancel symbol"
    if operation_type.startswith("upgrade_"):
        return "/confirm upgrade", "/cancel upgrade"
    if operation_type.startswith("model_"):
        return "/confirm model", "/cancel model"
    if operation_type.startswith("monitor_run"):
        return "/confirm monitor-run", "/cancel monitor-run"
    return "/confirm trade", "/cancel trade"


def _pending_operation_label(operation_type: str) -> str:
    return {
        "manual_open": "交易开仓",
        "manual_close": "交易平仓",
        "manual_expiry": "期权到期失效",
        "symbol_add": "监控新增",
        "symbol_edit": "监控修改",
        "symbol_remove": "监控删除",
        "upgrade_now": "立即升级",
        "model_use": "模型切换",
        "monitor_run_now": "监控执行",
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


def _render_symbol_resolve(data: dict[str, Any]) -> str:
    raw = _value(data.get("raw_input") or data.get("symbol"))
    canonical = _value(data.get("canonical_symbol"))
    if not bool(data.get("resolved")) or canonical == "-":
        message = str(data.get("message") or "").strip()
        return message or f"无法识别标的：{raw}。"
    market = _value(data.get("market"))
    currency = _value(data.get("currency"))
    futu_code = _value(data.get("futu_code"))
    details = "，".join(item for item in (market, currency, futu_code) if item != "-")
    suffix = f"（{details}）" if details else ""
    if raw != "-" and raw != canonical:
        return f"{raw} -> {canonical}{suffix}。"
    return f"{canonical}{suffix}。"


def _render_candidate_filter_explain(data: dict[str, Any]) -> str:
    symbol = _value(data.get("canonical_symbol") or data.get("symbol"))
    raw = _value(data.get("raw_symbol"))
    trace_count = data.get("trace_count")
    try:
        count = int(trace_count)
    except Exception:
        count = 0

    lines: list[str] = []
    title_symbol = symbol if symbol != "-" else raw
    if count <= 0:
        lines.append(f"没有找到 {title_symbol} 的开仓候选快照记录，不能判断确定原因。")
    else:
        lines.append(f"{title_symbol} 开仓候选诊断：{count} 条快照记录。")
    if raw != "-" and symbol != "-" and raw != symbol:
        lines.append(f"输入已解析：{raw} -> {symbol}。")

    scope = data.get("scope") if isinstance(data.get("scope"), dict) else {}
    account = _value(scope.get("account") or data.get("account"))
    if account != "-":
        lines.append(f"扫描范围：account={account}；这里的 account 只表示扫描/运行范围，不是标的身份字段。")

    functions = [item for item in data.get("functions") or [] if isinstance(item, dict)]
    observed = [item for item in functions if str(item.get("status") or "") != "not_observed"]
    for item in observed[:8]:
        function_name = _value(item.get("function"))
        status = _value(item.get("status"))
        rejection_reasons = [reason for reason in item.get("rejection_reasons") or [] if isinstance(reason, dict)]
        reason_counts = item.get("rejection_reason_counts") if isinstance(item.get("rejection_reason_counts"), dict) else {}
        if not reason_counts:
            reason_counts = item.get("reason_counts") if isinstance(item.get("reason_counts"), dict) else {}
        labels = item.get("reason_labels") if isinstance(item.get("reason_labels"), dict) else {}
        reason_text = _format_rejection_reasons(rejection_reasons) or _format_reason_counts(reason_counts, labels=labels)
        line = f"- {function_name}: {status}"
        if reason_text:
            line += f"；拒绝原因 {reason_text}"
        event = _first_candidate_event(item)
        if event:
            line += f"；{event}"
        lines.append(line)
    if count > 0 and not observed:
        lines.append("已读取开仓候选快照，但没有观察到匹配的策略记录。")

    lines.append("数据来源：OM sealed opening candidate snapshot")
    return "\n".join(lines)


def _format_rejection_reasons(reasons: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for reason in reasons:
        label = _value(reason.get("label") or reason.get("rule"))
        if label == "-":
            continue
        try:
            count = int(reason.get("count"))
        except Exception:
            count = 1
        parts.append(f"{label} x{count}")
    return "，".join(parts)


def _format_reason_counts(reason_counts: dict[str, Any], *, labels: dict[str, Any] | None = None) -> str:
    parts: list[str] = []
    for key, value in reason_counts.items():
        rule = _value(key)
        if rule == "-":
            continue
        label = _value((labels or {}).get(rule)) if labels else "-"
        if label == "-":
            label = rule
        try:
            count = int(value)
        except Exception:
            count = 1
        parts.append(f"{label} x{count}")
    return "，".join(parts)


def _first_candidate_event(item: dict[str, Any]) -> str:
    events = [event for event in item.get("events") or [] if isinstance(event, dict)]
    if not events:
        return ""
    event = events[0]
    metric = _value(event.get("metric_value"))
    threshold = _value(event.get("threshold"))
    message = _value(event.get("message"))
    rule_label = _value(event.get("rule_label"))
    details: list[str] = []
    if rule_label != "-":
        details.append(rule_label)
    if metric != "-" or threshold != "-":
        details.append(f"metric={metric}, threshold={threshold}")
    if message != "-":
        details.append(message)
    return "；".join(details)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _render_cash_headroom(data: dict[str, Any], _tool_result: dict[str, Any]) -> str:
    account = _value(data.get("account"))
    account_prefix = "" if account == "-" else f"{account} 账户 "
    unavailable_reason = str(data.get("cash_secured_unavailable_reason") or "").strip()
    if data.get("cash_secured_usage_reliable") is False:
        reason = f"：{unavailable_reason}" if unavailable_reason else "。"
        return f"{account_prefix}CSP 担保金是否超过现金加货基暂时不能确认{reason}\n数据来源：OM cash headroom query".strip()

    used = _float_or_none(data.get("cash_secured_used_cny"))
    available = _float_or_none(data.get("cash_available_total_cny"))
    if used is None or available is None:
        return (
            f"{account_prefix}CSP 担保金是否超过现金加货基暂时不能确认：缺少 CNY 折算后的担保金或现金类资产。\n"
            "数据来源：OM cash headroom query"
        ).strip()

    free = _float_or_none(data.get("cash_free_total_cny"))
    if free is None:
        free = available - used
    exceeds = used > available
    conclusion = "已经超过" if exceeds else "没有超过"
    gap_label = "缺口" if exceeds else "余量"
    gap_value = abs(free)
    lines = [f"{account_prefix}CSP 担保金{conclusion}账户现有现金加货基。".strip()]
    lines.append(f"- CSP 已占用担保金：{_money(used, 'CNY')}")
    lines.append(f"- 现金加货基（全币种折算）：{_money(available, 'CNY')}")
    lines.append(f"- {gap_label}：{_money(gap_value, 'CNY')}")
    by_ccy = data.get("cash_secured_total_by_ccy")
    if isinstance(by_ccy, dict) and by_ccy:
        lines.append(f"- 担保金原币合计：{_format_ccy_amounts(by_ccy)}")
    source = str(data.get("cash_source") or "").strip()
    if source:
        lines.append(f"- 现金口径：{source}")
    lines.append("数据来源：OM cash headroom query")
    return "\n".join(lines)


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


def _render_option_performance(data: dict[str, Any]) -> str:
    period = _dict(data.get("period"))
    scope = _dict(data.get("scope"))
    quality = _dict(data.get("quality"))
    accounts = [str(item).strip() for item in _list(scope.get("accounts")) if str(item).strip()]
    brokers = [str(item).strip() for item in _list(scope.get("brokers")) if str(item).strip()]
    scope_label = "、".join(accounts) if accounts else "全部账户"
    if brokers:
        scope_label += f"；broker {'、'.join(brokers)}"
    period_kind = str(period.get("kind") or "-").strip()
    period_kind_label = {"mtd": "MTD", "ytd": "YTD"}.get(period_kind, period_kind)
    period_label = f"{period.get('start_date') or '-'} 至 {period.get('as_of_date') or '-'}"
    lines = [
        f"期权收益统计完成（{scope_label}，{period_kind_label}，{period_label}）：",
        f"- 期权净现金流：{_canonical_cashflow_text(data.get('option_net_cashflow'))}",
        f"- 卖方胜率：{_canonical_win_rate_text(data.get('sell_option_win_rate'))}",
        f"- 买方胜率：{_canonical_win_rate_text(data.get('buy_option_win_rate'))}",
        f"- 期权收益率：{_canonical_option_return_text(data.get('option_return'))}",
    ]
    missing = [str(item) for item in _list(quality.get("missing")) if str(item)]
    if missing:
        lines.append("缺失证据：" + "；".join(missing[:6]))
    lines.append("口径：仅统计期权净现金流、卖方/买方胜率与按担保资本计算的期权收益率；不提供 option PnL。")
    return "\n".join(lines)


def _canonical_cashflow_text(value: Any) -> str:
    by_currency = _dict(_dict(value).get("by_currency"))
    parts: list[str] = []
    for currency, raw in sorted(by_currency.items()):
        components = _dict(raw)
        component_text = []
        for key, label in (("total", "合计"), ("open", "未终止"), ("terminated", "已终止")):
            metric = _dict(components.get(key))
            amount = _float_or_none(metric.get("amount"))
            component_text.append(f"{label} {amount:,.2f}" if amount is not None else f"{label} -")
        parts.append(f"{str(currency).upper()} " + "，".join(component_text))
    return "；".join(parts) if parts else "-"


def _canonical_win_rate_text(value: Any) -> str:
    metric = _dict(value)
    rate = _float_or_none(metric.get("rate"))
    winning = metric.get("winning_contracts", 0)
    eligible = metric.get("eligible_contracts", 0)
    status = str(metric.get("status") or "").strip()
    rate_text = f"{rate * 100:.2f}%" if rate is not None else "-"
    suffix = "（不适用）" if status == "not_applicable" else ("（证据不完整）" if status == "partial" else "")
    return f"{rate_text}（{winning}/{eligible} 张）{suffix}"


def _canonical_option_return_text(value: Any) -> str:
    by_currency = _dict(_dict(value).get("by_currency"))
    parts: list[str] = []
    for currency, raw in sorted(by_currency.items()):
        metric = _dict(raw)
        rate = _float_or_none(metric.get("rate"))
        annualized_rate = _float_or_none(metric.get("annualized_rate"))
        status = str(metric.get("status") or "").strip()
        period_text = f"{rate * 100:.2f}%" if rate is not None else "-"
        annualized_text = f"{annualized_rate * 100:.2f}%" if annualized_rate is not None else "-"
        text = f"期间 {period_text}，年化 {annualized_text}"
        if status == "partial":
            text += "（证据不完整）"
        elif status == "not_applicable":
            text += "（不适用）"
        parts.append(f"{str(currency).upper()} {text}")
    return " / ".join(parts) if parts else "-"


def _format_ccy_amounts(values: dict[str, Any]) -> str:
    parts: list[str] = []
    for currency, amount in sorted(values.items()):
        try:
            parts.append(f"{str(currency).upper()} {float(amount):,.0f}")
        except Exception:
            continue
    return " + ".join(parts) if parts else "-"


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return "-"


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


def _render_assigned_stock_lifecycle(data: dict[str, Any]) -> str:
    rows = _list(data.get("rows") or data.get("assigned_stock_lots"))
    filters = _dict(data.get("filters"))
    source_label = str(data.get("source_label") or "OM 本地 SQLite assigned_stock_events + trade_events").strip()
    account = _value(filters.get("account") or "all")
    status = _value(filters.get("status") or "open")
    symbol = _value(filters.get("symbol"))
    scope_parts = [account if account != "-" else "全部账户", status, "指派正股"]
    if symbol != "-":
        scope_parts.append(symbol)
    scope = " · ".join(scope_parts)
    quote_refresh = _dict(data.get("quote_refresh"))
    if not rows:
        lines = [f"{scope}：0 条。"]
        refresh_status = str(quote_refresh.get("status") or "").strip()
        if refresh_status:
            lines.append(f"报价刷新：{refresh_status}")
        lines.append(f"数据源：{source_label}")
        return "\n".join(lines)

    lines = [f"{scope}：{len(rows)} 条"]
    summary_lines = _assigned_stock_summary_lines(rows)
    if summary_lines:
        lines.append("汇总（按币种）：")
        lines.extend(summary_lines)
    lines.append("明细：")
    show_account = _assigned_stock_should_show_account(rows, account)
    for idx, row_raw in enumerate(rows, start=1):
        lines.append(_assigned_stock_detail_line(idx, row_raw, show_account=show_account))
    review_notes: list[str] = []
    for item in _list(data.get("assigned_stock_review_rows"))[:5]:
        row = _dict(item)
        note = f"{_value(row.get('symbol'))} {_value(row.get('status'))}: {_value(row.get('message'))}"
        if note.strip():
            review_notes.append(note)
    if review_notes:
        lines.append("检查提示：")
        lines.extend(f"- {note}" for note in review_notes)
    refresh_status = str(quote_refresh.get("status") or "").strip()
    if refresh_status:
        source = _value(quote_refresh.get("quote_source"))
        lines.append(f"报价刷新：{refresh_status} source={source}")
    unusable_quote_symbols = _assigned_stock_unusable_quote_symbols(data=data, rows=rows)
    if unusable_quote_symbols:
        lines.append(
            f"缺口：缺少实时行情：{'、'.join(unusable_quote_symbols)}，不能计算当前正股浮盈亏和生命周期PnL。"
        )
    warnings = [str(item).strip() for item in _list(data.get("warnings")) if str(item).strip()]
    if warnings:
        lines.append("提示：" + "；".join(warnings[:3]))
    lines.append("口径：正股成本按真实交割价记录，不扣除 CSP 权利金；生命周期PnL 才包含权利金归因。")
    lines.append(f"数据源：{source_label}")
    return "\n".join(lines)


_ASSIGNED_STOCK_UNUSABLE_QUOTE_STATUSES = frozenset(
    {"missing", "missing_quote", "stale", "expired", "unknown", "error", "failed"}
)


def _assigned_stock_unusable_quote_symbols(*, data: dict[str, Any], rows: list[Any]) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()

    def append_symbol(raw: Any) -> None:
        symbol = _value(raw)
        if symbol == "-" or symbol in seen:
            return
        symbols.append(symbol)
        seen.add(symbol)

    quote_refresh = _dict(data.get("quote_refresh"))
    for symbol in _list(quote_refresh.get("missing_symbols")):
        append_symbol(symbol)

    for row_raw in rows:
        row = _dict(row_raw)
        status = str(row.get("quote_status") or "").strip().lower()
        if status in _ASSIGNED_STOCK_UNUSABLE_QUOTE_STATUSES:
            append_symbol(row.get("symbol"))

    for item in _list(data.get("assigned_stock_review_rows")):
        row = _dict(item)
        status = str(row.get("status") or "").strip().lower()
        if status in _ASSIGNED_STOCK_UNUSABLE_QUOTE_STATUSES:
            append_symbol(row.get("symbol"))

    return symbols


def _assigned_stock_should_show_account(rows: list[Any], account_filter: str) -> bool:
    normalized_filter = str(account_filter or "").strip().lower()
    if normalized_filter in {"", "-", "all", "全部账户"}:
        return True
    accounts: set[str] = set()
    for row_raw in rows:
        account = _value(_dict(row_raw).get("account"))
        if account != "-":
            accounts.add(account)
    return len(accounts) > 1


def _assigned_stock_detail_line(idx: int, row_raw: Any, *, show_account: bool) -> str:
    row = _dict(row_raw)
    currency = _value(row.get("currency"))
    symbol = _value(row.get("symbol"))
    account = _value(row.get("account"))
    label = f"{account} {symbol}" if show_account and account != "-" else symbol
    row_status = _value(row.get("status"))
    shares_remaining = _num(row.get("shares_remaining"))
    shares_sold = _num(row.get("shares_sold"))
    cost = _money(row.get("stock_cost_per_share"), currency)
    spot = _money(row.get("spot"), currency)
    unrealized = _money(row.get("assigned_stock_unrealized_pnl"), currency)
    realized = _money(row.get("assigned_stock_realized_pnl"), currency)
    premium = _money(row.get("option_premium_attribution"), currency)
    lifecycle = _money(row.get("assignment_lifecycle_pnl"), currency)

    holding = f"剩余 {shares_remaining} 股"
    if shares_sold not in {"-", "0"}:
        holding += f"，已卖 {shares_sold} 股"

    parts = [
        f"{idx}. {label}",
        row_status,
        holding,
        f"成本 {cost}/股",
        f"spot {spot}",
    ]
    quote_status = _value(row.get("quote_status"))
    if _assigned_stock_should_show_quote_status(row=row, spot_text=spot, quote_status=quote_status):
        parts.append(f"quote={quote_status}")
    parts.append(f"正股浮盈亏 {unrealized}")
    if not _is_zero_number(row.get("assigned_stock_realized_pnl")):
        parts.append(f"正股已实现 {realized}")
    if not _is_zero_number(row.get("option_premium_attribution")):
        parts.append(f"权利金归因 {premium}")
    parts.append(f"生命周期PnL {lifecycle}")
    return " · ".join(parts)


def _assigned_stock_should_show_quote_status(*, row: dict[str, Any], spot_text: str, quote_status: str) -> bool:
    normalized = quote_status.strip().lower()
    if normalized not in {"", "-", "fresh", "not_required"}:
        return True
    row_status = _value(row.get("status")).lower()
    return spot_text == "-" and row_status in {"open", "partially_sold"}


def _assigned_stock_summary_lines(rows: list[Any]) -> list[str]:
    if len(rows) <= 1:
        return []
    grouped: dict[str, dict[str, float]] = {}
    counts: dict[str, int] = {}
    fields = {
        "remaining_stock_cost_basis": "剩余成本",
        "remaining_market_value": "市值",
        "assigned_stock_unrealized_pnl": "正股浮盈亏",
        "assigned_stock_realized_pnl": "正股已实现",
        "option_premium_attribution": "权利金归因",
        "assignment_lifecycle_pnl": "生命周期PnL",
    }
    for row_raw in rows:
        row = _dict(row_raw)
        currency = _value(row.get("currency"))
        if currency == "-":
            currency = ""
        bucket = grouped.setdefault(currency, {})
        counts[currency] = counts.get(currency, 0) + 1
        for field in fields:
            value = row.get(field)
            if value is None:
                continue
            try:
                amount = float(value)
            except Exception:
                continue
            bucket[field] = bucket.get(field, 0.0) + amount
    lines: list[str] = []
    for currency in sorted(grouped):
        bucket = grouped[currency]
        if not bucket:
            continue
        parts = [
            f"{fields[field]} {_money(bucket[field], currency)}"
            for field in fields
            if field in bucket
        ]
        if not parts:
            continue
        prefix = _value(currency) if currency else "未标币种"
        lines.append(f"- {prefix} · {counts.get(currency, 0)} 条：" + "，".join(parts))
    return lines


def _is_zero_number(value: Any) -> bool:
    if value is None:
        return True
    try:
        return abs(float(value)) < 1e-9
    except Exception:
        return False


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
    recommendation = str(row.get("recommendation_state") or "").strip().lower()
    return {
        "close": "建议平仓",
        "hold": "继续持有",
        "not_evaluable": "暂无法评估",
    }.get(recommendation, "-")


def _close_advice_reason(row: dict[str, Any]) -> str:
    return _value(row.get("reason"))


def _close_advice_metric_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    capture = row.get("net_capture_ratio")
    if capture is not None:
        parts.append(f"净捕获 {_pct(capture)}")
    opening_credit = row.get("opening_net_credit")
    if opening_credit is not None:
        parts.append(f"开仓净权利金 {_money(opening_credit, row.get('currency'))}")
    close_cost = row.get("all_in_close_cost")
    if close_cost is not None:
        parts.append(f"全成本买回 {_money(close_cost, row.get('currency'))}")
    close_cost_ratio = row.get("close_cost_ratio")
    if close_cost_ratio is not None:
        parts.append(f"买回成本/行权本金 {_pct(close_cost_ratio)}")
    remaining = row.get("remaining_term_ratio")
    if remaining is not None:
        parts.append(f"剩余期限占比 {_pct(remaining)}")
    spread = row.get("spread_ratio")
    if spread is not None:
        parts.append(f"价差 {_pct(spread)}")
    if row.get("dte") is not None:
        parts.append(f"DTE {_num(row.get('dte'))}")
    if isinstance(row.get("is_otm"), bool):
        parts.append(f"价外 {'是' if row['is_otm'] else '否'}")
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
            f"{_value(entry.get('kind') or 'log')} "
            f"exists={_yes_no(entry.get('exists'))} "
            f"tail={_value(entry.get('tail_line_count'))}"
        )
        error = str(entry.get("error_code") or "").strip()
        if error:
            lines.append(f"  error_code: {error}")
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


def _money(value: Any, currency: Any) -> str:
    if value is None:
        return "-"
    ccy = _value(currency)
    if ccy == "-":
        ccy = ""
    try:
        amount = float(value)
    except Exception:
        return _value(value)
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return f"{ccy} {text}" if ccy else text


_CanonicalRenderer = Callable[[dict[str, Any], dict[str, Any]], str]

_CANONICAL_RENDERERS: dict[str, _CanonicalRenderer] = {
    "option_performance": lambda data, _tool_result: _render_option_performance(data),
    "position_rows": lambda data, _tool_result: _render_positions(data),
    "assigned_stock_lifecycle": lambda data, _tool_result: _render_assigned_stock_lifecycle(data),
    "position_exit_analysis": lambda data, _tool_result: _render_position_exit_analysis(data),
    "runtime_runs": lambda data, _tool_result: _render_runs(data),
    "runtime_logs": lambda data, _tool_result: _render_logs(data),
    "runtime_status": _render_runtime_status,
    "healthcheck": _render_healthcheck,
    "config_validate": _render_config_validate,
    "symbol_config": lambda data, _tool_result: _render_symbol_config(data),
    "symbol_resolve": lambda data, _tool_result: _render_symbol_resolve(data),
    "candidate_filter_explain": lambda data, _tool_result: _render_candidate_filter_explain(data),
    "cash_headroom": _render_cash_headroom,
}
