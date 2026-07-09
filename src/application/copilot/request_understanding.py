from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from src.application.copilot.contracts import CapabilityHintDefinition, CopilotRequest


_SYMBOL_RE = re.compile(r"\b(?:[A-Z]{1,6}|\d{4}\.HK|[A-Z]{1,5}\.[A-Z]{1,3})\b")
_HK_NUMERIC_SYMBOL_RE = re.compile(r"\b(?!20\d{2}\b)(\d{4})\b")
_MONTH_RE = re.compile(r"(20\d{2}\s*年\s*\d{1,2}\s*月|20\d{2}-\d{1,2}|\d{1,2}\s*月)")
_MONTH_LIST_RE = re.compile(
    r"(?:(20\d{2})\s*年\s*)?(\d{1,2}(?:\s*(?:[、,，/和及与&+]|(?:\s+and\s+))\s*\d{1,2})+)\s*月",
    re.IGNORECASE,
)
_MONTH_RANGE_RE = re.compile(r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*(?:-|~|至|到)\s*(\d{1,2})\s*月")
_ENGLISH_MONTH_ALIASES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_CHINESE_MONTH_ALIASES = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}
_ENGLISH_MONTH_PATTERN = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_ENGLISH_MONTH_CONNECTOR = r"(?:\s*(?:,|/|&|\+|-|~)\s*|\s+(?:and|to|through|until)\s+)"
_ENGLISH_MONTH_RANGE_CONNECTOR_RE = re.compile(r"(?:-|~|\bto\b|\bthrough\b|\buntil\b)", re.IGNORECASE)
_ENGLISH_MONTH_SEQUENCE_RE = re.compile(
    rf"\b(?:(?:20\d{{2}})\s+)?(?:{_ENGLISH_MONTH_PATTERN})(?:\s+20\d{{2}})?"
    rf"(?:{_ENGLISH_MONTH_CONNECTOR}(?:{_ENGLISH_MONTH_PATTERN})(?:\s+20\d{{2}})?)+\b",
    re.IGNORECASE,
)
_ENGLISH_MONTH_TOKEN_RE = re.compile(
    rf"\b(?:(20\d{{2}})\s+)?({_ENGLISH_MONTH_PATTERN})(?:\s+(20\d{{2}}))?\b",
    re.IGNORECASE,
)
_CHINESE_MONTH_TOKEN_RE = re.compile(r"(十一|十二|十|一|二|三|四|五|六|七|八|九)\s*月")
_NON_SYMBOL_TERMS = {"PUT", "CALL"}


@dataclass(frozen=True)
class CapabilityHint:
    name: str
    source: str
    reason: str


@dataclass(frozen=True)
class RequestUnderstanding:
    scope: dict[str, Any]
    capability_hints: tuple[CapabilityHint, ...]
    scope_sources: dict[str, str]
    parser_name: str = "thin_request_understanding"

    @property
    def capabilities(self) -> set[str]:
        return {hint.name for hint in self.capability_hints}

    def trace(self) -> dict[str, Any]:
        return {
            "understanding_parser": self.parser_name,
            "requested_capabilities": sorted(self.capabilities),
            "capability_sources": [
                {"capability": hint.name, "source": hint.source, "reason": hint.reason}
                for hint in self.capability_hints
            ],
            "scope_sources": dict(self.scope_sources),
        }


def understand_request(
    request: CopilotRequest,
    *,
    capability_hints: Iterable[CapabilityHintDefinition],
    reference_year: int | None = None,
) -> RequestUnderstanding:
    message = request.user_message
    scope, scope_sources = _normalize_scope(request, reference_year=reference_year)
    inferred_capability_hints = _infer_capability_hints(message, capability_hints)
    return RequestUnderstanding(
        scope=scope,
        capability_hints=tuple(inferred_capability_hints),
        scope_sources=scope_sources,
    )


def _normalize_scope(
    request: CopilotRequest,
    *,
    reference_year: int | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    message = request.user_message
    config_key = _scope_string(request.explicit_scope.config_key)
    symbol, symbol_source = _first_present(
        _scope_string(request.explicit_scope.symbol),
        "explicit_scope.symbol",
        _extract_symbol(message, config_key=config_key),
        "message.symbol",
    )
    explicit_month = _scope_string(request.explicit_scope.month)
    message_months = [] if explicit_month else _normalized_message_months(message, reference_year=reference_year)
    if explicit_month:
        month, month_source = explicit_month, "explicit_scope.month"
    elif len(message_months) == 1:
        month, month_source = message_months[0], "message.month"
    else:
        month, month_source = None, None
    normalized_month, used_reference_year = _normalize_month(month, reference_year=reference_year)
    scope = {
        "config_key": config_key.lower() if config_key else None,
        "symbol": symbol.upper() if symbol else None,
        "month": normalized_month,
    }
    if len(message_months) > 1:
        scope["month_candidates"] = message_months
    scope_sources: dict[str, str] = {}
    if config_key:
        scope_sources["config_key"] = "explicit_scope.config_key"
    if symbol_source:
        scope_sources["symbol"] = symbol_source
    if month_source and normalized_month:
        scope_sources["month"] = month_source
    if len(message_months) > 1:
        scope_sources["month_candidates"] = "message.months"
    if used_reference_year or (
        not explicit_month and bool(message_months) and any(_month_uses_reference_year(item) for item in _extract_months(message))
    ):
        scope_sources["month_reference_year"] = "service.reference_year"
    return scope, scope_sources


def _first_present(first: Any, first_source: str, second: Any, second_source: str) -> tuple[Any, str | None]:
    if first:
        return first, first_source
    if second:
        return second, second_source
    return None, None


def _scope_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _extract_symbol(message: str, *, config_key: str | None = None) -> str | None:
    for match in _SYMBOL_RE.finditer(message):
        symbol = match.group(0)
        if symbol.upper() not in _NON_SYMBOL_TERMS:
            return symbol
    if str(config_key or "").strip().casefold() == "hk":
        match = _HK_NUMERIC_SYMBOL_RE.search(message)
        if match:
            return f"{match.group(1)}.HK"
    return None


def _extract_month(message: str) -> str | None:
    months = _extract_months(message)
    return months[0] if months else None


def _extract_months(message: str) -> list[str]:
    positioned: list[tuple[int, int, str]] = []
    shorthand_spans = _shorthand_month_spans(message)
    for match in _MONTH_RE.finditer(message):
        if any(start <= match.start() and match.end() <= end for start, end in shorthand_spans):
            continue
        positioned.append((match.start(), 0, re.sub(r"\s+", "", match.group(1))))
    positioned.extend(_extract_shorthand_months(message))
    positioned.extend(_extract_english_months(message))
    positioned.extend(_extract_chinese_months(message))
    return _dedupe([item for _start, _order, item in sorted(positioned)])


def _shorthand_month_spans(message: str) -> list[tuple[int, int]]:
    spans = [(match.start(), match.end()) for match in _MONTH_LIST_RE.finditer(message)]
    spans.extend((match.start(), match.end()) for match in _MONTH_RANGE_RE.finditer(message))
    spans.extend((match.start(), match.end()) for match in _ENGLISH_MONTH_SEQUENCE_RE.finditer(message))
    return spans


def _extract_shorthand_months(message: str) -> list[tuple[int, int, str]]:
    months: list[tuple[int, int, str]] = []
    for match in _MONTH_LIST_RE.finditer(message):
        year = match.group(1)
        for index, number in enumerate(re.findall(r"\d{1,2}", match.group(2))):
            months.append((match.start(), index, _raw_month_with_optional_year(number, year)))
    for match in _MONTH_RANGE_RE.finditer(message):
        year = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3))
        if 1 <= start <= 12 and 1 <= end <= 12:
            if start <= end:
                months.extend(
                    (match.start(), index, _raw_month_with_optional_year(str(number), year))
                    for index, number in enumerate(range(start, end + 1))
                )
            else:
                months.extend(
                    (
                        (match.start(), 0, _raw_month_with_optional_year(str(start), year)),
                        (match.start(), 1, _raw_month_with_optional_year(str(end), year)),
                    )
                )
    return months


def _raw_month_with_optional_year(month: str, year: str | None) -> str:
    return f"{year}年{month}月" if year else f"{month}月"


def _extract_english_months(message: str) -> list[tuple[int, int, str]]:
    months: list[tuple[int, int, str]] = []
    sequence_spans = [(match.start(), match.end()) for match in _ENGLISH_MONTH_SEQUENCE_RE.finditer(message)]
    for match in _ENGLISH_MONTH_SEQUENCE_RE.finditer(message):
        months.extend(_english_months_from_sequence(match))
    for match in _ENGLISH_MONTH_TOKEN_RE.finditer(message):
        if any(start <= match.start() and match.end() <= end for start, end in sequence_spans):
            continue
        month = _english_month_number(match.group(2))
        if month is None:
            continue
        year = match.group(1) or match.group(3)
        if _is_unscoped_modal_may(match.group(2), year):
            continue
        months.append((match.start(), 0, _raw_month_with_optional_year(str(month), year)))
    return months


def _english_months_from_sequence(match: re.Match[str]) -> list[tuple[int, int, str]]:
    tokens = list(_ENGLISH_MONTH_TOKEN_RE.finditer(match.group(0)))
    explicit_years = {
        year
        for token in tokens
        for year in (token.group(1), token.group(3))
        if year
    }
    inherited_year = next(iter(explicit_years)) if len(explicit_years) == 1 else None
    if len(tokens) == 2 and _ENGLISH_MONTH_RANGE_CONNECTOR_RE.search(match.group(0)):
        start = _english_month_number(tokens[0].group(2))
        end = _english_month_number(tokens[1].group(2))
        if start is not None and end is not None and start <= end:
            start_year = tokens[0].group(1) or tokens[0].group(3) or inherited_year
            end_year = tokens[1].group(1) or tokens[1].group(3) or inherited_year
            if start_year == end_year:
                return [
                    (match.start(), index, _raw_month_with_optional_year(str(month), start_year))
                    for index, month in enumerate(range(start, end + 1))
                ]
    months: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        month = _english_month_number(token.group(2))
        if month is None:
            continue
        year = token.group(1) or token.group(3) or inherited_year
        months.append((match.start() + token.start(), index, _raw_month_with_optional_year(str(month), year)))
    return months


def _english_month_number(value: str) -> int | None:
    return _ENGLISH_MONTH_ALIASES.get(value.casefold())


def _extract_chinese_months(message: str) -> list[tuple[int, int, str]]:
    months: list[tuple[int, int, str]] = []
    for match in _CHINESE_MONTH_TOKEN_RE.finditer(message):
        month = _CHINESE_MONTH_ALIASES.get(match.group(1))
        if month is not None:
            months.append((match.start(), 0, f"{month}月"))
    return months


def _is_unscoped_modal_may(month_name: str, year: str | None) -> bool:
    return month_name.casefold() == "may" and year is None and month_name != "May"


def _normalized_message_months(message: str, *, reference_year: int | None) -> list[str]:
    months: list[str] = []
    for item in _extract_months(message):
        normalized, _used_reference_year = _normalize_month(item, reference_year=reference_year)
        if normalized and normalized not in months:
            months.append(normalized)
    return months


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _month_uses_reference_year(value: Any) -> bool:
    raw = str(value or "").replace(" ", "")
    return bool(re.fullmatch(r"\d{1,2}月", raw) or re.fullmatch(r"(十一|十二|十|一|二|三|四|五|六|七|八|九)月", raw))


def _normalize_month(value: Any, *, reference_year: int | None) -> tuple[str | None, bool]:
    raw = str(value or "").strip()
    if not raw:
        return None, False
    year_month = re.fullmatch(r"(20\d{2})-(\d{1,2})", raw)
    if year_month:
        month_num = int(year_month.group(2))
        if 1 <= month_num <= 12:
            return f"{year_month.group(1)}-{month_num:02d}", False
        return None, False
    chinese_year_month = re.fullmatch(r"(20\d{2})年(\d{1,2})月", raw.replace(" ", ""))
    if chinese_year_month:
        month_num = int(chinese_year_month.group(2))
        if 1 <= month_num <= 12:
            return f"{chinese_year_month.group(1)}-{month_num:02d}", False
        return None, False
    month_match = re.fullmatch(r"(\d{1,2})月", raw.replace(" ", ""))
    if month_match:
        month_num = int(month_match.group(1))
        if 1 <= month_num <= 12 and reference_year:
            return f"{reference_year}-{month_num:02d}", True
    return None, False


def _infer_capability_hints(
    message: str,
    capability_hints: Iterable[CapabilityHintDefinition],
) -> list[CapabilityHint]:
    hints: list[CapabilityHint] = []
    for definition in capability_hints:
        if _matches_activation_terms(message, definition.activation_terms):
            hints.append(
                CapabilityHint(definition.capability, "message", definition.activation_reason or "activation_hint")
            )
    return hints


def _matches_activation_terms(message: str, activation_terms: tuple[tuple[str, ...], ...]) -> bool:
    normalized = message.casefold()
    for group in activation_terms:
        terms = tuple(term.casefold().strip() for term in group if isinstance(term, str) and term.strip())
        if terms and all(term in normalized for term in terms):
            return True
    return False
