"""Deterministic facts and fallback wording for structured API results.

The model narrates the fact card; this module supplies every number that may
be shown to the group.  It formats existing fields and never recomputes a
price, fee, yield or recommendation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo

CLARIFICATION_NOTE = "（未执行实时查询，以上为澄清或说明，不含实时数据。）"

ROLL_METHODS = frozenset({"evaluate_roll_down", "evaluate_roll_up"})
FACT_CARD_CANDIDATES = 10
FALLBACK_CANDIDATES = 5

QUOTE_STATUS_TEXT = {
    "current": "当前行情",
    "last_known": "最近已知报价",
    "delayed": "延迟行情",
    "unavailable": "暂时拿不到行情",
}

MARKET_TEXT = {"US": "美股", "HK": "港股"}

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

OUTCOME_TEXT = {
    "found": "找到了候选",
    "none": "没有符合条件的候选",
    "unable": "这次没能完成查询",
}

RECOMMENDATION_TEXT = {
    "hold": "建议继续持有",
    "close": "建议平仓",
    "not_evaluable": "暂时算不出明确建议",
}

EVALUATION_TEXT = {
    "priced": "已定价",
    "not_evaluable": "无法评估",
    "historical_reference": "历史参考",
}

ECONOMIC_RESULT_TEXT = {
    "positive_net_credit": "净收入为正",
    "non_positive_net_credit": "净收入不为正",
    "excluded": "已排除",
}

REASON_TEXT = {
    "market_closed": "现在市场休市",
    "closed_market_reference": "休市参考数据",
    "closed_market_reference_mismatch": "休市参考数据不一致",
    "closed_market_calendar_unavailable": "休市日历暂不可用",
    "not_started": "查询还没开始",
    "date_changed": "交易日刚切换，需要重新查",
    "market_unavailable": "暂时拿不到市场行情",
    "market_data_provider": "行情数据源不可用",
    "enumeration_incomplete": "到期日数据不完整",
    "evidence_unavailable": "行情证据不可用",
    "underlier_unavailable": "标的行情不可用",
    "underlier_quote_rights": "没有标的行情权限",
    "quote_rights": "没有这个市场的行情权限",
    "query_range_exceeded": "查询范围超出限制",
    "no_contracts_in_range": "这个范围里没有合约",
    "risk_delta_unavailable": "Delta 数据不可用",
    "fx_unavailable": "汇率数据不可用",
    "contract_ineligible": "合约不符合条件",
    "policy_rejected": "不符合公共策略条件",
    "contract_identity_invalid": "合约信息无效",
    "contract_identity_conflict": "合约信息冲突",
    "candidate_evidence_invalid": "候选行情证据不完整",
    "no_qualified_candidate": "没有满足经济条件的候选",
    "search_scope_not_proven_complete": "搜索范围还没证明完整",
    "opening_underlier_not_ready": "标的行情还没准备好",
    "underlier_observation_not_ready": "标的行情还没准备好",
    "duplicate_obligation_not_verified": "持仓义务还没有核实",
    "actual_share_count_not_verified": "实际股数还没有核实",
    "all_unclosed_short_call_coverage_not_verified": "未平仓 Call 的股票覆盖还没有核实",
    "underlier_last_price_invalid": "标的最新价无效",
    "quote_snapshot_future": "行情快照时间异常",
    "quote_snapshot_time": "行情快照时间异常",
    "option_suspended": "期权合约停牌",
    "underlier_suspended": "标的停牌",
    "option_sec_status_not_normal": "期权合约状态异常",
    "underlier_sec_status_not_normal": "标的证券状态异常",
}

# Tokens a reply may use to name the reason.  They are derived from the same
# owner codes that the fact card translates, so the guard and the card cannot
# disagree about what the member was told.
REASON_KEYWORDS = {
    "market_closed": ("休市", "开盘", "盘前", "盘后", "收市"),
    "closed_market_reference": ("休市", "参考"),
    "closed_market_reference_mismatch": ("参考", "不一致"),
    "closed_market_calendar_unavailable": ("休市日历", "日历"),
    "not_started": ("还没开始", "未开始", "没开始"),
    "date_changed": ("交易日", "重新查"),
    "market_unavailable": ("行情", "不可用", "拿不到"),
    "market_data_provider": ("行情源", "数据源"),
    "enumeration_incomplete": ("不完整", "到期日", "数据"),
    "evidence_unavailable": ("证据", "行情", "不可用"),
    "underlier_unavailable": ("标的", "行情"),
    "underlier_quote_rights": ("行情权限", "权限"),
    "quote_rights": ("行情权限", "权限"),
    "query_range_exceeded": ("范围", "超出"),
    "no_contracts_in_range": ("没有合约", "范围"),
    "risk_delta_unavailable": ("Delta", "不可用"),
    "fx_unavailable": ("汇率",),
    "contract_ineligible": ("合约", "不符合"),
    "policy_rejected": ("策略", "不符合"),
    "contract_identity_invalid": ("合约信息", "无效"),
    "contract_identity_conflict": ("合约信息", "冲突"),
    "candidate_evidence_invalid": ("证据", "不完整"),
    "no_qualified_candidate": ("没有", "满足", "经济条件"),
    "search_scope_not_proven_complete": ("范围", "不完整", "没证明"),
    "opening_underlier_not_ready": ("标的", "没准备好", "未就绪"),
    "underlier_observation_not_ready": ("标的", "没准备好", "未就绪"),
    "duplicate_obligation_not_verified": ("核实", "持仓"),
    "actual_share_count_not_verified": ("核实", "股数"),
    "all_unclosed_short_call_coverage_not_verified": ("核实", "覆盖"),
    "underlier_last_price_invalid": ("最新价", "无效"),
    "quote_snapshot_future": ("时间异常", "快照"),
    "quote_snapshot_time": ("时间异常", "快照"),
    "option_suspended": ("停牌",),
    "underlier_suspended": ("停牌",),
    "option_sec_status_not_normal": ("状态异常", "合约"),
    "underlier_sec_status_not_normal": ("状态异常", "标的"),
}

CLOSE_METRIC_LABELS = {
    "opening_gross_credit": ("开仓毛收入", "money"),
    "opening_net_credit": ("开仓净收入", "money"),
    "all_in_close_cost": ("全部平仓成本", "money"),
    "net_capture_ratio": ("净捕获比例", "pct"),
    "close_cost_ratio": ("平仓成本比例", "pct"),
    "remaining_term_ratio": ("剩余期限比例", "pct"),
    "capital_basis": ("保证金基数", "money"),
    "remaining_max_annualized_return": ("剩余最高年化", "pct"),
    "estimated_pnl_if_close_net": ("平仓后预计净盈亏", "money"),
}

CANDIDATE_ANNUALIZED_KEYS = (
    "incremental_annualized_return", "annualized_net_return", "annualized_return",
)


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


def _market_state_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.upper() in {"N/A", "NA", "NAN", "UNKNOWN", "NULL", "-"}:
        return None
    return MARKET_STATE_LABELS.get(text, "未知")


def _strip_disclaimer_variants(text: str) -> str:
    """Remove a model-written copy of the clarification disclaimer.

    Only parenthetical or whole-line disclaimer copies are removed, so a
    genuine clarification sentence such as “我还没有执行实时查询，先确认……” keeps
    its meaning.  The host appends exactly one note afterwards.
    """
    cleaned = str(text)
    while CLARIFICATION_NOTE in cleaned:
        cleaned = cleaned.replace(CLARIFICATION_NOTE, "")
    cleaned = re.sub(
        r"[（(][^（）()]*(?:未执行实时查询|不含实时数据)[^（）()]*[）)]",
        "", cleaned)
    kept: list[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip().strip("（）()").strip()
        if (len(stripped) <= 80
                and ("未执行实时查询" in stripped or "不含实时数据" in stripped)
                and stripped.startswith(
                    ("以上", "本回复", "未执行实时查询", "不含实时数据"))):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def strip_clarification_note(text: str | None) -> str:
    """Remove every clarification disclaimer occurrence and trailing blanks."""
    if not text:
        return ""
    lines = [line.rstrip() for line in _strip_disclaimer_variants(str(text)).splitlines()]
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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_number(value: Any) -> bool:
    return _is_number(value) and value > 0


def _symbol_text(data: dict[str, Any]) -> str | None:
    symbol = data.get("symbol")
    if symbol in (None, ""):
        return None
    market = MARKET_TEXT.get(str(data.get("market") or "").upper())
    return f"{symbol}（{market}）" if market else str(symbol)


def _position_text(position: Any) -> str | None:
    if not isinstance(position, dict):
        return None
    symbol = position.get("symbol")
    contract = position.get("contract") if isinstance(position.get("contract"), dict) else {}
    option_type = {"put": "Put", "call": "Call"}.get(
        str(contract.get("option_type") or "").lower(), "")
    bits = [str(symbol)] if symbol else []
    if contract.get("expiration"):
        bits.append(str(contract["expiration"]))
    if contract.get("strike") is not None:
        bits.append(f"{_number(contract['strike'], 2)} {option_type}".strip())
    elif option_type:
        bits.append(option_type)
    side = {"short": "空头", "long": "多头"}.get(str(position.get("side") or ""))
    if side:
        bits.append(side)
    if position.get("contracts") is not None:
        bits.append(f"{position['contracts']} 张")
    return " ".join(bit for bit in bits if bit) or None


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
        bits.append(f"最新价 {_number(quote['last_price'], 4)}")
        bits.append(f"最新价时间 {price_time}")
    for label, key in (("买一", "bid"), ("卖一", "ask")):
        if _positive_number(quote.get(key)):
            bits.append(f"{label} {_number(quote[key], 4)}")
    if _is_number(quote.get("delta")):
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


def _missing_reason_text(data: dict[str, Any]) -> str | None:
    codes = [str(code) for code in data.get("missing_requirements") or []]
    texts: list[str] = []
    for code in codes:
        text = REASON_TEXT.get(code, "数据条件不满足")
        if text not in texts:
            texts.append(text)
    return "、".join(texts) if texts else None


def _filter_source(filters: dict[str, Any], key: str) -> str:
    return "用户指定" if filters.get(key) == "request" else "公共策略默认"


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
    return "筛选条件：" + "；".join(bits)


def _candidate_facts(candidate: dict[str, Any], *, opening: bool = False) -> str:
    metrics = candidate.get("metrics")
    if not isinstance(metrics, dict):
        metrics = candidate
    if opening:
        contract_symbol = candidate.get("contract")
        expiration = candidate.get("expiry")
        strike = candidate.get("strike")
        evidence = candidate.get("evidence")
        quote = evidence if isinstance(evidence, dict) else {}
    else:
        contract = candidate.get("option_contract")
        if not isinstance(contract, dict):
            contract = {}
        contract_symbol = candidate.get("contract_symbol")
        expiration = candidate.get("expiration") or contract.get("expiration")
        strike = candidate.get("strike") if candidate.get("strike") is not None \
            else contract.get("strike")
        quote = candidate
    bits = [
        _text(contract_symbol),
        f"到期 {_text(expiration)}",
        f"行权价 {_number(strike, 2)}",
        f"买 {_number(quote.get('bid'), 4)} / 卖 {_number(quote.get('ask'), 4)}",
    ]
    if _is_number(candidate.get("delta")):
        bits.append(f"Delta {_number(candidate['delta'], 3)}")
    if candidate.get("dte") is not None:
        bits.append(f"DTE {candidate['dte']}")
    annualized = next((metrics.get(key) for key in CANDIDATE_ANNUALIZED_KEYS
                       if _is_number(metrics.get(key))), None)
    if annualized is not None:
        bits.append(f"年化 {_number(annualized * 100, 2)}%")
    economic = candidate.get("economic_result")
    if economic is not None:
        bits.append(ECONOMIC_RESULT_TEXT.get(str(economic), "经济结果已评估"))
    if _is_number(candidate.get("estimated_sell_fee")):
        bits.append(f"预计卖出手续费 {_number(candidate['estimated_sell_fee'], 2)}")
    return "；".join(bits)


def _candidate_line(index: int, candidate: dict[str, Any], *, opening: bool = False) -> str:
    return f"{index}. {_candidate_facts(candidate, opening=opening)}"


def _quality_lines(data: dict[str, Any]) -> list[str]:
    quality = data.get("data_quality")
    if not isinstance(quality, dict):
        return []
    lines: list[str] = []
    if quality.get("mode") == "closed_market_reference":
        notice = str(quality.get("notice") or "").strip()
        lines.append(
            f"说明：{notice}" if notice
            else "说明：休市参考，用的是最近一个完成交易日的数据，不是实时可成交报价。")
        if quality.get("reference_date"):
            lines.append(f"参考交易日：{quality['reference_date']}")
    state = _market_state_text(quality.get("market_state"))
    if state:
        lines.append(f"现在市场状态：{state}")
    zone, zone_label = _zone_for_market(data.get("market"))
    observed = _receipt_time(quality.get("underlying_observed_at"), zone, zone_label)
    if observed:
        lines.append(f"标的行情时间：{observed}")
    return lines


def _reason_lines(data: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for code in data.get("reason_codes") or []:
        text = REASON_TEXT.get(str(code), "数据条件不满足")
        if text not in texts:
            texts.append(text)
    if not texts:
        quality = data.get("data_quality")
        if isinstance(quality, dict) and quality.get("mode") == "closed_market_reference":
            texts.append(REASON_TEXT["closed_market_reference"])
    return [f"原因：{'、'.join(texts)}"] if texts else []


def _fee_sentence(fee: dict[str, Any] | None) -> str | None:
    if not isinstance(fee, dict):
        return None
    open_fee = fee.get("estimated_open_fee")
    close_fee = fee.get("estimated_close_fee")
    if not _is_number(open_fee) and not _is_number(close_fee):
        return None
    bits: list[str] = []
    if _is_number(open_fee):
        bits.append(f"开仓费约 {_number(open_fee, 2)}")
    if _is_number(close_fee):
        bits.append(f"平仓费约 {_number(close_fee, 2)}")
    return ("费用：" + "，".join(bits)
            + "；按富途标准费用模型估算，不是账户实际扣费。")


def _metric_facts(metrics: dict[str, Any] | None) -> str:
    if not isinstance(metrics, dict):
        return ""
    bits: list[str] = []
    for key, (label, kind) in CLOSE_METRIC_LABELS.items():
        value = metrics.get(key)
        if not _is_number(value):
            continue
        if kind == "pct":
            bits.append(f"{label} {_number(value * 100, 2)}%")
        else:
            bits.append(f"{label} {_number(value, 2)}")
    return "；".join(bits)


def _roll_contract_error(data: dict[str, Any]) -> str | None:
    if data.get("analysis_kind") != "conditional":
        return None
    coverage = data.get("stock_coverage")
    if (not isinstance(coverage, dict)
            or coverage.get("verification_status") != "not_verified"):
        return "结果合同不一致：缺少未经核验的股票覆盖字段，拒绝展示。"
    if not data.get("unverified_conditions"):
        return "结果合同不一致：缺少未核验条件，拒绝展示。"
    return None


def result_contract_error(method: str, data: dict[str, Any]) -> str | None:
    """Return the refusal text for a result we may not show at all."""
    if method in ROLL_METHODS:
        return _roll_contract_error(data)
    return None


def _candidate_block(data: dict[str, Any], *, card: bool,
                     opening: bool = False) -> list[str]:
    candidates = [item for item in (data.get("candidates") or [])
                  if isinstance(item, dict)]
    limit = FACT_CARD_CANDIDATES if card else FALLBACK_CANDIDATES
    shown = candidates[:limit]
    lines: list[str] = []
    for index, candidate in enumerate(shown, start=1):
        if card:
            lines.append(f"候选 {index}：{_candidate_facts(candidate, opening=opening)}")
        else:
            lines.append(_candidate_line(index, candidate, opening=opening))
    omitted = data.get("omitted")
    omitted_count = (omitted if isinstance(omitted, int)
                     and not isinstance(omitted, bool) and omitted > 0 else 0)
    if len(candidates) > len(shown):
        lines.append(f"共 {len(candidates) + omitted_count} 个候选，"
                     f"这里列出前 {len(shown)} 个。")
    elif omitted_count:
        lines.append(f"另有 {omitted_count} 个候选没有列出。")
    return lines


def _roll_kind(data: dict[str, Any]) -> str:
    return "Put 下移" if data.get("analysis_kind") == "roll_down" else "Call 上移"


def _roll_scope_line(data: dict[str, Any]) -> str | None:
    scope = data.get("scope")
    if not isinstance(scope, dict):
        return None
    bits: list[str] = []
    if scope.get("max_new_expiration"):
        bits.append(f"最晚新到期日 {scope['max_new_expiration']}")
    if scope.get("strike_boundary") is not None:
        label = "最小新行权价" if data.get("analysis_kind") == "roll_down" \
            else "最大新行权价"
        bits.append(f"{label} {_number(scope['strike_boundary'], 2)}")
    return "搜索范围：" + "；".join(bits) if bits else None


def _quote_card(data: dict[str, Any]) -> str:
    status_key = str(data.get("quote_status") or "")
    lines = ["查询结果：实时行情"]
    symbol = _symbol_text(data)
    if symbol:
        lines.append(f"标的：{symbol}")
    lines.append(f"状态：{QUOTE_STATUS_TEXT.get(status_key, '行情状态未知')}")
    lines.append(f"标的行情：{_primary_quote(data.get('underlier'), data.get('market'))}")
    requested = (data.get("effective_parameters") or {}).get("contract_requested")
    if requested:
        lines.append(f"合约行情：{_primary_quote(data.get('option'), data.get('market'))}")
    missing = _missing_reason_text(data)
    if missing:
        lines.append(f"缺少：{missing}")
    if status_key == "last_known":
        lines.append("说明：以上是最近已知报价及其时间，不是当前可成交价格。")
    elif status_key == "delayed":
        lines.append("说明：以上是延迟行情，不是实时成交价。")
    if status_key == "unavailable":
        lines.append(f"原因：{missing or '暂时拿不到行情'}")
    return "\n".join(lines)


def _candidates_card(data: dict[str, Any]) -> str:
    lines = ["查询结果：候选查询"]
    symbol = _symbol_text(data)
    if symbol:
        lines.append(f"标的：{symbol}")
    outcome = str(data.get("outcome") or "")
    lines.append(f"结果：{OUTCOME_TEXT.get(outcome, '查询结果未知')}")
    lines.extend(_quality_lines(data))
    filter_line = effective_filter_line(data)
    if filter_line:
        lines.append(filter_line)
    lines.extend(_candidate_block(data, card=True, opening=True))
    lines.extend(_reason_lines(data))
    return "\n".join(lines)


def _close_card(data: dict[str, Any]) -> str:
    lines = ["查询结果：平仓评估"]
    position = _position_text(data.get("position"))
    if position:
        lines.append(f"持仓：{position}")
    state = str(data.get("recommendation_state") or "")
    evaluation = str(data.get("evaluation_status") or "")
    conclusion = RECOMMENDATION_TEXT.get(state, "评估状态未知")
    if evaluation:
        conclusion += f"（{EVALUATION_TEXT.get(evaluation, evaluation)}）"
    lines.append(f"结论：{conclusion}")
    lines.append(f"原合约行情：{_primary_quote(data.get('quote'), data.get('market'))}")
    metrics = _metric_facts(data.get("metrics"))
    if metrics:
        lines.append(f"关键指标：{metrics}")
    fee = _fee_sentence(data.get("fee_model"))
    if fee:
        lines.append(fee)
    if data.get("reason"):
        lines.append(f"理由：{data['reason']}")
    lines.extend(_reason_lines(data))
    if not lines[-1].startswith(("原因：", "理由：")) and state == "not_evaluable":
        lines.append("原因：数据不足，暂时算不出明确建议。")
    return "\n".join(lines)


def _roll_card(data: dict[str, Any]) -> str:
    error = _roll_contract_error(data)
    if error:
        return error
    lines = [f"查询结果：移仓搜索（{_roll_kind(data)}）"]
    position = _position_text(data.get("position"))
    if position:
        lines.append(f"持仓：{position}")
    lines.append(f"结果：{OUTCOME_TEXT.get(str(data.get('outcome') or ''), '查询结果未知')}")
    lines.append(f"原合约行情：{_primary_quote(data.get('old_quote'), data.get('market'))}")
    if data.get("analysis_kind") == "conditional":
        lines.append("条件分析：股票覆盖未经核实，不构成正式账户建议。")
    lines.extend(_candidate_block(data, card=True))
    scope = _roll_scope_line(data)
    if scope:
        lines.append(scope)
    if data.get("candidates"):
        lines.append("费用说明：候选的预计卖出手续费按富途标准费用模型估算，不是账户实际扣费。")
    lines.extend(_reason_lines(data))
    return "\n".join(lines)


def fact_card(method: str, data: dict[str, Any]) -> str:
    """A display-ready Chinese fact sheet for the model to narrate.

    Every value the reply may mention comes from here; the guard checks the
    reply against this text.
    """
    if method == "query_quotes":
        return _quote_card(data)
    if method == "query_candidates":
        return _candidates_card(data)
    if method == "evaluate_close":
        return _close_card(data)
    if method in ROLL_METHODS:
        return _roll_card(data)
    raise ValueError("unsupported group-query method")


def render_quotes(data: dict[str, Any]) -> str:
    status_key = str(data.get("quote_status") or "")
    lead = QUOTE_STATUS_TEXT.get(status_key, "行情状态未知")
    symbol = data.get("symbol")
    lines = [f"{symbol}：{lead}" if symbol else lead]
    lines.append(f"标的行情：{_primary_quote(data.get('underlier'), data.get('market'))}")
    requested = (data.get("effective_parameters") or {}).get("contract_requested")
    if requested:
        lines.append(f"合约行情：{_primary_quote(data.get('option'), data.get('market'))}")
    missing = _missing_reason_text(data)
    if missing:
        lines.append(f"缺少：{missing}")
    if status_key == "last_known":
        lines.append("以上是最近已知报价及其时间，不是当前可成交价格。")
    elif status_key == "delayed":
        lines.append("以上是延迟行情，不是实时成交价。")
    if status_key == "unavailable":
        lines.append(f"原因：{missing or '暂时拿不到行情'}")
    return "\n".join(lines)


def render_candidates(data: dict[str, Any]) -> str:
    outcome = str(data.get("outcome") or "")
    lead = OUTCOME_TEXT.get(outcome, "查询结果未知")
    symbol = data.get("symbol")
    lines = [f"{symbol}：{lead}" if symbol else lead]
    lines.extend(_quality_lines(data))
    filter_line = effective_filter_line(data)
    if filter_line:
        lines.append(filter_line)
    lines.extend(_candidate_block(data, card=False, opening=True))
    if not data.get("candidates") and outcome == "found":
        lines.append("没有符合条件的候选。")
    lines.extend(_reason_lines(data))
    return "\n".join(lines)


def render_close(data: dict[str, Any]) -> str:
    lines: list[str] = []
    position = _position_text(data.get("position"))
    if position:
        lines.append(position)
    state = str(data.get("recommendation_state") or "")
    evaluation = str(data.get("evaluation_status") or "")
    conclusion = RECOMMENDATION_TEXT.get(state, "评估状态未知")
    if evaluation:
        conclusion += f"（{EVALUATION_TEXT.get(evaluation, evaluation)}）"
    lines.append(f"平仓评估：{conclusion}")
    lines.append(f"原合约行情：{_primary_quote(data.get('quote'), data.get('market'))}")
    metrics = _metric_facts(data.get("metrics"))
    if metrics:
        lines.append(f"关键指标：{metrics}")
    fee = _fee_sentence(data.get("fee_model"))
    if fee:
        lines.append(fee)
    if data.get("reason"):
        lines.append(f"理由：{data['reason']}")
    lines.extend(_reason_lines(data))
    if not lines[-1].startswith(("原因：", "理由：")) and state == "not_evaluable":
        lines.append("原因：数据不足，暂时算不出明确建议。")
    return "\n".join(lines)


def render_roll(data: dict[str, Any]) -> str:
    error = _roll_contract_error(data)
    if error:
        return error
    symbol = data.get("symbol")
    lead = OUTCOME_TEXT.get(str(data.get("outcome") or ""), "查询结果未知")
    heading = f"{symbol} 移仓（{_roll_kind(data)}）：{lead}" if symbol \
        else f"移仓（{_roll_kind(data)}）：{lead}"
    lines = [heading]
    position = _position_text(data.get("position"))
    if position:
        lines.append(position)
    lines.append(f"原合约行情：{_primary_quote(data.get('old_quote'), data.get('market'))}")
    if data.get("analysis_kind") == "conditional":
        lines.append("条件分析：股票覆盖未经核实，不构成正式账户建议。")
    lines.extend(_candidate_block(data, card=False))
    scope = _roll_scope_line(data)
    if scope:
        lines.append(scope)
    if data.get("candidates"):
        lines.append("费用说明：候选的预计卖出手续费按富途标准费用模型估算，不是账户实际扣费。")
    lines.extend(_reason_lines(data))
    return "\n".join(lines)


def render_result(method: str, data: dict[str, Any]) -> str:
    if method == "query_quotes":
        return render_quotes(data)
    if method == "query_candidates":
        return render_candidates(data)
    if method == "evaluate_close":
        return render_close(data)
    if method in ROLL_METHODS:
        return render_roll(data)
    raise ValueError("unsupported group-query method")


def render_admission_rejected(retry_after_seconds: int) -> str:
    return f"请求过于频繁，请约 {max(1, int(retry_after_seconds))} 秒后重试。"


def render_budget_exhausted(reason: str) -> str:
    return f"本次查询在限制内未能完成（{reason}），请缩小范围或稍后重试。"


__all__ = [
    "CLARIFICATION_NOTE",
    "REASON_KEYWORDS",
    "ROLL_METHODS",
    "effective_filter_line",
    "fact_card",
    "finalize_clarification",
    "render_admission_rejected",
    "render_budget_exhausted",
    "render_candidates",
    "render_close",
    "render_quotes",
    "render_result",
    "render_roll",
    "result_contract_error",
    "strip_clarification_note",
]
