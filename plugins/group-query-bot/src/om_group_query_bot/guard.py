"""Host checks for a model-written result reply.

The model narrates the fact card.  This module proves it stayed on that card:
numbers, dates and contract symbols must come from the facts or the member's
own message, the required safety clauses must be present, and internal field
names never reach the group.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any

from .render import CLARIFICATION_NOTE, REASON_KEYWORDS, ROLL_METHODS

MAX_REPLY_CHARS = 700
MAX_REPLY_LINES = 24

NOT_CURRENT_TOKENS = (
    "不是当前", "非当前", "不是现在", "非实时", "不是实时",
    "最近已知", "历史", "上一次",
)
ESTIMATE_TOKENS = ("估算", "预估", "估计")
NON_ACCOUNT_TOKENS = (
    "非实际", "不是实际", "非账户", "不是账户", "不代表实际", "以账户为准",
)
UNVERIFIED_TOKENS = (
    "未核验", "未经核实", "未核实", "没有核实", "待核实", "未经确认", "不构成",
)
COVERAGE_TOKENS = ("覆盖", "股数", "正股", "股票")
GENERIC_REASON_TOKENS = (
    "查不", "无法", "不能", "没有", "不可用", "不完整", "不足", "不满足", "暂不",
)

JARGON_LITERALS = (
    "候选状态", "数据口径", "标的观察时间", "展示 0 条", "market_closed",
    "effective_filters", "回显", "已受理", "missing_requirements",
    "reason_codes", "quote_status", "evaluation_status",
    "recommendation_state", "unverified_conditions", "stock_coverage",
    "查询结果：",
)
JARGON_PATTERNS = (
    re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+"),
    re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),
    re.compile(r"展示\s*\d+\s*条"),
    re.compile(r"筛选："),
    re.compile(r"行情状态："),
    re.compile(r"候选状态："),
    re.compile(r"数据口径："),
)

# Chinese replies glue tokens to the surrounding text ("涨到999.99",
# "合约US.NVDA...").  Boundaries may only reject ASCII word characters, or
# the checks would depend on spacing the model is free to omit.
_AWORD = "A-Za-z0-9_"

_SYMBOL_RE = re.compile(
    rf"(?<![{_AWORD}.])("
    r"[A-Za-z]{1,4}\.[A-Za-z0-9]*\d[A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*"
    r"|[A-Z]{1,4}\.[A-Z][A-Z0-9]*"
    r"|\d{3,5}\.(?:HK|US)"
    rf")(?![{_AWORD}.])"
)
_ISO_DATE_RE = re.compile(
    rf"(?<![{_AWORD}.])\d{{4}}-\d{{2}}-\d{{2}}(?![{_AWORD}])"
)
_CN_DATE_RE = re.compile(
    rf"(?<![{_AWORD}])(\d{{4}})\s*年\s*(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*[日号]"
)
_MD_DATE_RE = re.compile(
    rf"(?<![{_AWORD}年])(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*[日号]"
)
_NUMBER_RE = re.compile(
    rf"(?<![{_AWORD}.])"
    r"([+\-−]?)"
    r"(\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.(\d+))?"
    r"\s*(万|亿|％|%)?"
)
_PLAIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_MULTIPLIERS = {"万": 10_000.0, "亿": 100_000_000.0}


@dataclass(frozen=True)
class ReplyVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _has_jargon(text: str) -> bool:
    if _contains_any(text, JARGON_LITERALS):
        return True
    return any(pattern.search(text) for pattern in JARGON_PATTERNS)


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("{", "[")) or '{"' in text


def _has_table(text: str) -> bool:
    return re.search(r"^\s*\|.*\|\s*$", text, re.MULTILINE) is not None


def _scrub_identifiers(text: str) -> str:
    text = _SYMBOL_RE.sub(" ", text)
    text = _ISO_DATE_RE.sub(" ", text)
    text = _CN_DATE_RE.sub(" ", text)
    text = _MD_DATE_RE.sub(" ", text)
    return text


def _symbols(text: str) -> tuple[str, ...]:
    return tuple(match.group(1) for match in _SYMBOL_RE.finditer(text))


def _plain_words(text: str) -> frozenset[str]:
    return frozenset(_PLAIN_WORD_RE.findall(text))


def _dates(text: str) -> list[date]:
    allowed: list[date] = []
    for match in _ISO_DATE_RE.finditer(text):
        try:
            allowed.append(date.fromisoformat(match.group(0)))
        except ValueError:
            continue
    for match in _CN_DATE_RE.finditer(text):
        try:
            allowed.append(date(int(match.group(1)), int(match.group(2)),
                                int(match.group(3))))
        except ValueError:
            continue
    return allowed


def _short_dates(text: str) -> list[tuple[int, int]]:
    return [(int(match.group(1)), int(match.group(2)))
            for match in _MD_DATE_RE.finditer(text)]


def _numbers(text: str) -> list[tuple[float, int]]:
    found: list[tuple[float, int]] = []
    for match in _NUMBER_RE.finditer(_scrub_identifiers(text)):
        sign = -1.0 if match.group(1) in {"-", "−"} else 1.0
        whole = match.group(2).replace(",", "")
        fraction = match.group(3) or ""
        try:
            value = float(f"{whole}.{fraction}" if fraction else whole)
        except ValueError:
            continue
        value *= _MULTIPLIERS.get(match.group(4) or "", 1.0) * sign
        found.append((value, len(fraction)))
    return found


def _supported_number(value: float, decimals: int,
                      allowed: list[tuple[float, int]]) -> bool:
    # A reply may round a displayed value to the precision it shows, but it
    # may not introduce a digit string that the facts never contained.
    for allowed_value, _ in allowed:
        tolerance = 0.5 * (10 ** -decimals) + 1e-9
        if abs(value - allowed_value) <= tolerance + 1e-9 * max(1.0, abs(allowed_value)):
            return True
    return False


def _symbol_reasons(text: str, fact_card_text: str, user_text: str) -> list[str]:
    allowed = set(_symbols(fact_card_text)) | set(_symbols(user_text))
    allowed_words = _plain_words(fact_card_text) | _plain_words(user_text)
    reasons: list[str] = []
    for symbol in _symbols(text):
        if symbol in allowed:
            continue
        if symbol.split(".")[-1] in allowed_words:
            continue
        reasons.append(f"invented_symbol:{symbol}")
    return reasons[:3]


def _date_reasons(text: str, fact_card_text: str, user_text: str,
                  now: datetime) -> list[str]:
    allowed = set(_dates(fact_card_text)) | set(_dates(user_text)) | {now.date()}
    allowed_short = {(value.month, value.day) for value in allowed}
    allowed_short |= set(_short_dates(fact_card_text)) | set(_short_dates(user_text))
    reasons: list[str] = []
    candidates = [(match, None) for match in _ISO_DATE_RE.finditer(text)]
    candidates += [(match, True) for match in _CN_DATE_RE.finditer(text)]
    for match, has_year in candidates:
        try:
            value = (date(int(match.group(1)), int(match.group(2)),
                          int(match.group(3)))
                     if has_year else date.fromisoformat(match.group(0)))
        except ValueError:
            reasons.append(f"invented_date:{match.group(0)}")
            continue
        if value not in allowed:
            reasons.append(f"invented_date:{value.isoformat()}")
    for month, day in _short_dates(text):
        if (month, day) not in allowed_short:
            reasons.append(f"invented_date:{month:02d}-{day:02d}")
    return reasons[:3]


def _number_reasons(text: str, fact_card_text: str, user_text: str) -> list[str]:
    allowed = _numbers(fact_card_text) + _numbers(user_text)
    reasons: list[str] = []
    for value, decimals in _numbers(text):
        if _supported_number(value, decimals, allowed):
            continue
        display = f"{value:.{decimals}f}" if decimals else f"{value:.0f}"
        reasons.append(f"invented_number:{display}")
    return reasons[:3]


def _reason_tokens(data: dict[str, Any]) -> tuple[str, ...]:
    codes = [str(code) for code in (data.get("reason_codes") or [])]
    codes.extend(str(code) for code in (data.get("missing_requirements") or []))
    quality = data.get("data_quality")
    if isinstance(quality, dict) and quality.get("mode") == "closed_market_reference":
        codes.append("closed_market_reference")
    tokens: list[str] = []
    for code in codes:
        for token in REASON_KEYWORDS.get(code, ()):
            if token not in tokens:
                tokens.append(token)
    return tuple(tokens) or GENERIC_REASON_TOKENS


def _reason_required(method: str, data: dict[str, Any]) -> bool:
    if method == "query_candidates":
        return str(data.get("outcome") or "") == "unable"
    if method == "evaluate_close":
        return (str(data.get("recommendation_state") or "") == "not_evaluable"
                or str(data.get("evaluation_status") or "") == "not_evaluable")
    if method in ROLL_METHODS:
        return str(data.get("outcome") or "") == "unable"
    if method == "query_quotes":
        return str(data.get("quote_status") or "") == "unavailable"
    return False


def _fee_required(method: str, data: dict[str, Any]) -> bool:
    if method == "evaluate_close":
        fee = data.get("fee_model")
        if not isinstance(fee, dict):
            return False
        return any(isinstance(fee.get(key), (int, float))
                   and not isinstance(fee.get(key), bool)
                   for key in ("estimated_open_fee", "estimated_close_fee"))
    if method in ROLL_METHODS:
        return any(isinstance(candidate, dict)
                   and isinstance(candidate.get("estimated_sell_fee"), (int, float))
                   and not isinstance(candidate.get("estimated_sell_fee"), bool)
                   for candidate in (data.get("candidates") or []))
    return False


def _required_reasons(text: str, method: str, data: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if (method == "query_quotes"
            and str(data.get("quote_status") or "") == "last_known"
            and not _contains_any(text, NOT_CURRENT_TOKENS)):
        reasons.append("missing_last_known_notice")
    if _fee_required(method, data):
        if not _contains_any(text, ESTIMATE_TOKENS):
            reasons.append("missing_fee_estimate")
        if not _contains_any(text, NON_ACCOUNT_TOKENS):
            reasons.append("missing_non_account_fee")
    if (method in ROLL_METHODS
            and data.get("analysis_kind") == "conditional"
            and not (_contains_any(text, UNVERIFIED_TOKENS)
                     and _contains_any(text, COVERAGE_TOKENS))):
        reasons.append("missing_unverified_conditions")
    if _reason_required(method, data) and not _contains_any(text, _reason_tokens(data)):
        reasons.append("missing_reason")
    return reasons


def check_result_reply(text: str, *, method: str, data: dict[str, Any],
                       fact_card_text: str, user_text: str,
                       now: datetime) -> ReplyVerdict:
    clean = (text or "").strip()
    if not clean:
        return ReplyVerdict(False, ("empty",))
    reasons: list[str] = []
    if len(clean) > MAX_REPLY_CHARS:
        reasons.append("too_long")
    if len(clean.splitlines()) > MAX_REPLY_LINES:
        reasons.append("too_many_lines")
    if _contains_any(clean, (CLARIFICATION_NOTE, "未执行实时查询", "不含实时数据")):
        reasons.append("clarification_note")
    if _has_jargon(clean):
        reasons.append("internal_jargon")
    if _has_table(clean):
        reasons.append("markdown_table")
    if _looks_like_json(clean):
        reasons.append("raw_json")
    reasons.extend(_symbol_reasons(clean, fact_card_text, user_text))
    reasons.extend(_date_reasons(clean, fact_card_text, user_text, now))
    reasons.extend(_number_reasons(clean, fact_card_text, user_text))
    reasons.extend(_required_reasons(clean, method, data))
    return ReplyVerdict(not reasons, tuple(reasons))


def _describe_reason(reason: str) -> str:
    labels = {
        "empty": "回复是空的",
        "too_long": "回复太长",
        "too_many_lines": "回复行数太多",
        "clarification_note": "数据回复里不要加“未执行实时查询”的说明",
        "internal_jargon": "出现了内部字段名或英文状态代码",
        "markdown_table": "不要用 Markdown 表格",
        "raw_json": "不要输出 JSON",
        "missing_last_known_notice": "要说明这是最近已知报价、不是当前实时价",
        "missing_fee_estimate": "要说明费用是估算",
        "missing_non_account_fee": "要说明不是账户实际扣费",
        "missing_unverified_conditions": "要说明股票覆盖未经核实",
        "missing_reason": "要说明为什么这次查不了",
    }
    if reason in labels:
        return labels[reason]
    if reason.startswith("invented_number:"):
        return f"数字 {reason.split(':', 1)[1]} 不在查询结果里"
    if reason.startswith("invented_symbol:"):
        return f"合约代码 {reason.split(':', 1)[1]} 不在查询结果里"
    if reason.startswith("invented_date:"):
        return f"日期 {reason.split(':', 1)[1]} 不在查询结果里"
    return "回复不符合要求"


def correction_message(verdict: ReplyVerdict) -> str:
    items = "；".join(_describe_reason(reason) for reason in verdict.reasons)
    return (
        f"系统校验未通过，请重写这条回复：{items}。"
        "只根据上一条查询结果，用简短、口语的中文回答；只引用结果里出现过的数字、"
        "日期和合约代码；不要输出字段名、英文代码、JSON 或表格，也不要自己计算新的数字。")
