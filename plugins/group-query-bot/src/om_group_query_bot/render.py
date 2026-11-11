"""Deterministic rendering of structured API results.

Key numbers and states shown to group members come only from this module,
never from model prose.  It formats existing fields; it never recomputes a
price, fee, yield or recommendation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

CLARIFICATION_NOTE = "（未执行实时查询，以上为澄清或说明，不含实时数据。）"

QUOTE_STATUS_LABELS = {
    "current": "当前",
    "last_known": "最近已知",
    "delayed": "延迟",
    "unavailable": "不可用",
}

MARKET_ZONES = {
    "US": (ZoneInfo("America/New_York"), "纽约时间"),
    "HK": (ZoneInfo("Asia/Hong_Kong"), "香港时间"),
}

MARKET_STATE_LABELS = {
    "NONE": "未开盘/无交易",
    "MORNING": "早盘",
    "AFTERNOON": "午盘",
    "AFTERNOON_END": "午盘结束",
    "AUCTION": "集合竞价",
    "TRADE_AUCTION": "交易竞价",
    "PRE_MARKET_BEGIN": "盘前",
    "PRE_MARKET_END": "盘前结束",
    "AFTER_HOURS_BEGIN": "盘后",
    "AFTER_HOURS_END": "盘后结束",
    "CLOSED": "休市",
    "REST": "休市或午间",
    "WAITING_OPEN": "待开盘",
    "HK_CAS": "收市竞价",
    "CLOSE_AUCTION": "收市竞价",
    "TRADE_AT_LAST": "按最新价交易",
    "NIGHT_OPEN": "夜盘",
    "NIGHT_END": "夜盘结束",
    "NIGHT": "夜盘",
    "OVERNIGHT_BEGIN": "隔夜盘",
    "OVERNIGHT_END": "隔夜盘结束",
    "OVERNIGHT": "隔夜盘",
}

OUTCOME_LABELS = {
    "found": "找到候选",
    "none": "无可展示候选",
    "unable": "无法完成搜索",
}


def _market_zone(quote: dict[str, Any]) -> tuple[Any, str | None]:
    symbol = str(quote.get("contract_symbol") or "")
    prefix = symbol.split(".", 1)[0].upper() if "." in symbol else ""
    return MARKET_ZONES.get(prefix, (None, None))


def _zone_for_market(market: Any) -> tuple[Any, str | None]:
    return MARKET_ZONES.get(str(market or "").upper(), (None, None))


def _receipt_time(value: Any, zone: Any, zone_label: str | None) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text
    if zone is None:
        return f"{parsed.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}（UTC）"
    return f"{parsed.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S')}（{zone_label}）"


def _price_time(value: Any, zone: Any, zone_label: str | None) -> str | None:
    """Format a last-trade time, or refuse a placeholder/invalid one.

    OpenD update times are normally market-local wall-clock strings without an
    offset.  An offset-aware value (some fixtures and derived rows use ISO)
    must be converted before it is labelled with the market zone; placeholders
    such as ``N/A`` fail the parse and are not shown as times.
    """
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # OpenD wall clock: already in the market's local time.  A date with
        # no clock component is not a last-trade time.
        if ":" not in text:
            return None
        return f"{text}（{zone_label}）" if zone_label else text
    if zone is None:
        return f"{parsed.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}（UTC）"
    return f"{parsed.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S')}（{zone_label}）"


def _evaluated_time(data: dict[str, Any]) -> str | None:
    zone, zone_label = _zone_for_market(data.get("market"))
    return _receipt_time(data.get("evaluated_at_utc"), zone, zone_label)


def _market_state_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.upper() in {"N/A", "NA", "NAN", "UNKNOWN", "NULL", "-"}:
        return None
    label = MARKET_STATE_LABELS.get(text)
    return f"{label}（{text}）" if label and label != text else text


def strip_clarification_note(text: str | None) -> str:
    """Remove every clarification disclaimer occurrence and trailing blanks."""
    if not text:
        return ""
    cleaned = str(text)
    while CLARIFICATION_NOTE in cleaned:
        cleaned = cleaned.replace(CLARIFICATION_NOTE, "")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def finalize_clarification(text: str | None) -> str:
    """Append exactly one disclaimer to a clarification reply."""
    prose = strip_clarification_note(text)
    if not prose:
        return CLARIFICATION_NOTE
    return f"{prose}\n\n{CLARIFICATION_NOTE}"


def _number(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "不可用"
    return f"{float(value):.{digits}f}"


def _text(value: Any) -> str:
    return str(value) if value not in (None, "") else "不可用"


def _positive_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and value > 0)


def _primary_quote(quote: dict[str, Any] | None, market: Any = None) -> str:
    if not isinstance(quote, dict):
        return "报价不可用"
    bits: list[str] = []
    zone, zone_label = _market_zone(quote)
    if zone is None:
        zone, zone_label = _zone_for_market(market)
    price_time = _price_time(quote.get("last_price_update_time"), zone, zone_label)
    # A last trade is only shown with its own timestamp; a placeholder time or
    # a zero/absent price is not a displayable quote.  Bid/ask are quote sides
    # and use the receipt time shown below.
    if _positive_number(quote.get("last_price")) and price_time:
        bits.append(f"最新 {_number(quote['last_price'], 4)}")
        bits.append(f"最新价时间 {price_time}")
    for label, key in (("买", "bid"), ("卖", "ask")):
        if _positive_number(quote.get(key)):
            bits.append(f"{label} {_number(quote[key], 4)}")
    if quote.get("delta") is not None:
        bits.append(f"Delta {_number(quote['delta'], 3)}")
    state = _market_state_text(quote.get("market_state"))
    if state:
        bits.append(f"市场状态 {state}")
    observed = _receipt_time(
        quote.get("observed_at_utc") or quote.get("snapshot_received_at_utc"),
        zone, zone_label)
    if observed:
        bits.append(f"取数时间 {observed}")
    return "；".join(bits) if bits else "报价不可用"


def render_quotes(data: dict[str, Any]) -> str:
    status = QUOTE_STATUS_LABELS.get(str(data.get("quote_status")), str(data.get("quote_status")))
    requested = (data.get("effective_parameters") or {}).get("contract_requested")
    lines = [f"行情状态：{status}"]
    lines.append(f"标的：{_primary_quote(data.get('underlier'), data.get('market'))}")
    if requested:
        lines.append(f"合约：{_primary_quote(data.get('option'), data.get('market'))}")
    missing = data.get("missing_requirements") or []
    if missing:
        lines.append("缺失：" + "、".join(str(item) for item in missing))
    if data.get("quote_status") == "last_known":
        lines.append("说明：以上为最近已知报价及其时间，非当前可成交状态。")
    return "\n".join(lines)


def _candidate_line(index: int, candidate: dict[str, Any]) -> str:
    metrics = candidate.get("metrics")
    if not isinstance(metrics, dict):
        metrics = candidate
    contract = candidate.get("option_contract")
    if not isinstance(contract, dict):
        contract = {}
    expiration = candidate.get("expiration") or contract.get("expiration")
    strike = candidate.get("strike") if candidate.get("strike") is not None \
        else contract.get("strike")
    bits = [
        f"{index}. {_text(candidate.get('contract_symbol'))}",
        f"到期 {_text(expiration)}",
        f"行权价 {_number(strike, 2)}",
        f"买 {_number(candidate.get('bid'), 4)}/卖 {_number(candidate.get('ask'), 4)}",
    ]
    if candidate.get("delta") is not None:
        bits.append(f"Delta {_number(candidate['delta'], 3)}")
    if candidate.get("dte") is not None:
        bits.append(f"DTE {candidate['dte']}")
    annualized = metrics.get("incremental_annualized_return")
    if annualized is None:
        annualized = metrics.get("annualized_net_return")
    if annualized is None:
        annualized = metrics.get("annualized_return")
    if annualized is not None:
        bits.append(f"年化 {_number(annualized * 100, 2)}%")
    if candidate.get("economic_result") is not None:
        bits.append(f"经济结果 {candidate['economic_result']}")
    return "；".join(bits)


def _filter_source(filters: dict[str, Any], key: str) -> str:
    return "用户指定" if filters.get(key) == "request" else "公共策略默认"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _range_text(low: Any, high: Any) -> str | None:
    low_ok = _is_number(low)
    high_ok = _is_number(high)
    if low_ok and high_ok:
        return f"{_number(low, 2)}–{_number(high, 2)}"
    if high_ok:
        return f"≤ {_number(high, 2)}"
    if low_ok:
        return f"≥ {_number(low, 2)}"
    return None


def _delta_filter_text(filters: dict[str, Any]) -> str | None:
    """Describe the *effective* Delta window, not the user's raw request.

    ``effective_filters.abs_delta_range`` keeps the requested range verbatim;
    the applied range is ``[min_abs_delta, max_abs_delta]`` after the public
    cap intersection (``option_candidates.prepare_public_query``).  Echoing the
    requested range would overstate what the candidates were filtered by.
    """
    effective = (filters.get("min_abs_delta"), filters.get("max_abs_delta"))
    requested = filters.get("abs_delta_range")
    text = _range_text(*effective)
    if text is None and isinstance(requested, list) and len(requested) == 2:
        text = _range_text(requested[0], requested[1])
    if text is None:
        return None
    source = _filter_source(filters, "max_abs_delta_source")
    if (source == "用户指定"
            and isinstance(requested, list) and len(requested) == 2
            and all(_is_number(value) for value in (*effective, *requested))
            and (float(effective[0]), float(effective[1]))
            != (float(requested[0]), float(requested[1]))):
        source += "，受公共策略上限约束"
    return f"Delta 绝对值 {text}（{source}）"


def effective_filter_line(data: dict[str, Any]) -> str | None:
    """Echo the filters actually applied, including public-policy defaults."""
    filters = data.get("effective_filters")
    if not isinstance(filters, dict):
        return None
    bits: list[str] = []
    dte = filters.get("dte")
    if (isinstance(dte, list) and len(dte) == 2
            and all(isinstance(value, int) and not isinstance(value, bool)
                    for value in dte)):
        bits.append(f"期限 {dte[0]}–{dte[1]} 天"
                    f"（{_filter_source(filters, 'dte_source')}）")
    delta_text = _delta_filter_text(filters)
    if delta_text:
        bits.append(delta_text)
    strike = filters.get("strike")
    if _is_number(strike):
        bits.append(f"行权价 {_number(strike, 2)}"
                    f"（{_filter_source(filters, 'strike_source')}）")
    else:
        strike_range = _range_text(filters.get("min_strike"),
                                   filters.get("max_strike"))
        if strike_range:
            bits.append(f"行权价 {strike_range}"
                        f"（{_filter_source(filters, 'strike_source')}）")
    limit = filters.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool):
        bits.append(f"展示上限 {limit}")
    if not bits:
        return None
    return "筛选：" + "；".join(bits)


def render_candidates(data: dict[str, Any]) -> str:
    outcome = OUTCOME_LABELS.get(str(data.get("outcome")), str(data.get("status")))
    lines = [f"候选状态：{outcome}"]
    quality = data.get("data_quality")
    if isinstance(quality, dict):
        if quality.get("mode") == "closed_market_reference":
            notice = str(quality.get("notice") or "").strip()
            lines.append(
                f"数据口径：{notice}" if notice
                else "数据口径：休市参考，基于最近已完成交易日；非实时可成交报价。")
            if quality.get("reference_date"):
                lines.append(f"参考交易日：{quality['reference_date']}")
        state = _market_state_text(quality.get("market_state"))
        if state:
            lines.append(f"市场状态：{state}")
        zone, zone_label = _zone_for_market(data.get("market"))
        observed = _receipt_time(quality.get("underlying_observed_at"), zone, zone_label)
        if observed:
            lines.append(f"标的观察时间：{observed}")
    filter_line = effective_filter_line(data)
    if filter_line:
        lines.append(filter_line)
    candidates = data.get("candidates") or []
    for index, candidate in enumerate(candidates, start=1):
        lines.append(_candidate_line(index, candidate))
    if not candidates:
        lines.append("没有可展示的候选。")
    lines.append(f"展示 {int(data.get('displayed') or 0)} 条，排除 "
                 f"{int(data.get('omitted') or 0)} 条。")
    for code in data.get("reason_codes") or []:
        lines.append(f"原因：{code}")
    return "\n".join(lines)


def _metrics_lines(metrics: dict[str, Any] | None) -> list[str]:
    if not isinstance(metrics, dict):
        return []
    lines = []
    for key, value in metrics.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{key}：{_number(value, 4)}")
        elif value not in (None, ""):
            lines.append(f"{key}：{value}")
    return lines


def render_close(data: dict[str, Any]) -> str:
    lines = [
        f"平仓评估：{_text(data.get('recommendation_state'))} / {_text(data.get('evaluation_status'))}",
        f"原合约报价：{_primary_quote(data.get('quote'), data.get('market'))}",
    ]
    lines.extend(_metrics_lines(data.get("metrics")))
    fee = data.get("fee_model") or {}
    if isinstance(fee, dict):
        lines.append(
            f"费用模型 {_text(fee.get('id') or fee.get('model_id'))}"
            f"@{_text(fee.get('version'))}："
            f"开仓费 {_number(fee.get('estimated_open_fee'), 2)}，"
            f"平仓费 {_number(fee.get('estimated_close_fee'), 2)}"
            f"（{_text(fee.get('fee_calc_status'))}）")
    lines.append("按富途标准费用模型估算，非账户实际费用。")
    if data.get("reason"):
        lines.append(f"理由：{data['reason']}")
    for code in data.get("reason_codes") or []:
        lines.append(f"原因：{code}")
    lines.append(f"评估时间：{_text(_evaluated_time(data))}")
    return "\n".join(lines)


def render_roll(data: dict[str, Any]) -> str:
    outcome = OUTCOME_LABELS.get(str(data.get("outcome")), str(data.get("outcome")))
    lines = [
        f"移仓搜索：{outcome}（{_text(data.get('status'))}）",
        f"原合约：{_primary_quote(data.get('old_quote'), data.get('market'))}",
    ]
    if data.get("analysis_kind") == "conditional":
        coverage = data.get("stock_coverage")
        if (not isinstance(coverage, dict)
                or coverage.get("verification_status") != "not_verified"):
            return "结果合同不一致：缺少未经核验的股票覆盖字段，拒绝展示。"
        if not data.get("unverified_conditions"):
            return "结果合同不一致：缺少未核验条件，拒绝展示。"
        lines.append("条件分析：股票覆盖未经核实，不构成正式账户建议。")
    candidates = data.get("candidates") or []
    for index, candidate in enumerate(candidates, start=1):
        lines.append(_candidate_line(index, candidate))
    if not candidates:
        lines.append("没有可展示的候选。")
    lines.append(f"展示 {int(data.get('displayed') or 0)} 条，排除 "
                 f"{int(data.get('omitted') or 0)} 条。")
    scope = data.get("scope") or {}
    if isinstance(scope, dict):
        for key, value in scope.items():
            lines.append(f"范围 {key}：{value}")
    for code in data.get("reason_codes") or []:
        lines.append(f"原因：{code}")
    lines.append("按富途标准费用模型估算，非账户实际费用。")
    lines.append(f"评估时间：{_text(_evaluated_time(data))}")
    return "\n".join(lines)


def render_result(method: str, data: dict[str, Any]) -> str:
    if method == "query_quotes":
        return render_quotes(data)
    if method == "query_candidates":
        return render_candidates(data)
    if method == "evaluate_close":
        return render_close(data)
    if method in {"evaluate_roll_down", "evaluate_roll_up"}:
        return render_roll(data)
    raise ValueError("unsupported group-query method")


def compose_reply(explanation: str | None, rendered: str | None) -> str:
    """Model prose may explain; the rendered block is the numeric authority."""
    prose = (explanation or "").strip()
    if rendered is None:
        return prose
    if not prose:
        return rendered
    return f"{prose}\n\n{rendered}"


def render_admission_rejected(retry_after_seconds: int) -> str:
    return f"请求过于频繁，请约 {max(1, int(retry_after_seconds))} 秒后重试。"


def render_budget_exhausted(reason: str) -> str:
    return f"本次查询在限制内未能完成（{reason}），请缩小范围或稍后重试。"


__all__ = [
    "CLARIFICATION_NOTE",
    "compose_reply",
    "effective_filter_line",
    "finalize_clarification",
    "render_admission_rejected",
    "render_budget_exhausted",
    "render_candidates",
    "render_close",
    "render_quotes",
    "render_result",
    "render_roll",
    "strip_clarification_note",
]
