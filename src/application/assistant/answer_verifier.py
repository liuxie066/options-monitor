from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from src.application.assistant.evidence import EvidenceBundle


_CURRENCY_AMOUNT_RE = re.compile(r"\b(USD|HKD|CNY)\s*([-+]?\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
_QUANTITY_RE = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)\s*(股|张|条|笔)")
_DATE_RE = re.compile(r"\b(20\d{2}[-/](?:0?[1-9]|1[0-2])(?:[-/](?:0?[1-9]|[12]\d|3[01]))?|20\d{2}年(?:0?[1-9]|1[0-2])月(?:[0-3]?\d日)?)")
_SYMBOL_RE = re.compile(r"\b(?:\d{4,5}\.HK|[A-Z]{1,6}(?:\.[A-Z]{1,3})?)\b")
_STATUS_RE = re.compile(r"\b(open|closed|partially_sold|fresh|missing_quote|stale|expired|previewed|applied|cancelled|failed)\b")
_LOSS_TOKENS = ("亏", "损", "loss", "negative")
_APPROX_TOKENS = ("约", "大约", "左右", "around", "approx")
_IGNORED_SYMBOL_TOKENS = {
    "API",
    "CNY",
    "HKD",
    "LLM",
    "OM",
    "PNL",
    "USD",
}
_STATUS_CUE_TOKENS = ("状态", "status", "quote", "行情", "预览", "确认", "取消", "写入")


@dataclass(frozen=True)
class AnswerVerificationResult:
    violations: tuple[dict[str, Any], ...]
    checked_claim_count: int
    supported_claim_count: int

    def public_payload(self) -> dict[str, Any]:
        return {
            "violations": [dict(item) for item in self.violations],
            "checked_claim_count": int(self.checked_claim_count),
            "supported_claim_count": int(self.supported_claim_count),
        }


def verify_response_against_evidence(response_text: str, *, evidence_bundle: EvidenceBundle | None) -> AnswerVerificationResult:
    if evidence_bundle is None:
        return AnswerVerificationResult(violations=(), checked_claim_count=0, supported_claim_count=0)
    allowed_currency_amounts = _allowed_currency_amounts(evidence_bundle)
    allowed_quantities = _allowed_quantities(evidence_bundle)
    allowed_dates = _allowed_dates(evidence_bundle)
    allowed_symbols = _allowed_symbols(evidence_bundle)
    allowed_statuses = _allowed_statuses(evidence_bundle)
    checked = 0
    supported = 0
    violations: list[dict[str, Any]] = []
    text = str(response_text or "")
    for match in _CURRENCY_AMOUNT_RE.finditer(text):
        currency = str(match.group(1) or "").upper()
        value = _parse_number(match.group(2))
        if value is None:
            continue
        currency_values = allowed_currency_amounts.get(currency)
        if not currency_values:
            continue
        checked += 1
        segment = _segment_for_match(text, match.start(), match.end())
        if _amount_supported(value, currency_values, segment=segment):
            supported += 1
            continue
        violations.append(
            {
                "type": "unsupported_contract_currency_amount",
                "claim": f"{currency} {match.group(2)}",
                "currency": currency,
                "evidence": "amount is not present in EvidenceBundle currency facts or reconciliation sums",
            }
        )
    for match in _QUANTITY_RE.finditer(text):
        value = _parse_number(match.group(1))
        unit_label = str(match.group(2) or "")
        if value is None:
            continue
        unit = _quantity_unit(unit_label)
        values = allowed_quantities.get(unit)
        if not values:
            continue
        checked += 1
        if _number_supported(value, values):
            supported += 1
            continue
        violations.append(
            {
                "type": "unsupported_contract_quantity",
                "claim": f"{match.group(1)}{unit_label}",
                "unit": unit,
                "evidence": "quantity is not present in EvidenceBundle facts or row counts",
            }
        )
    for match in _DATE_RE.finditer(text):
        normalized_date = _normalize_date(match.group(1))
        if not normalized_date or not allowed_dates:
            continue
        checked += 1
        if normalized_date in allowed_dates:
            supported += 1
            continue
        violations.append(
            {
                "type": "unsupported_contract_date",
                "claim": match.group(1),
                "evidence": "date is not present in EvidenceBundle date facts or scope",
            }
        )
    for match in _SYMBOL_RE.finditer(text):
        symbol = _normalize_symbol(match.group(0))
        if not symbol or symbol in _IGNORED_SYMBOL_TOKENS or not allowed_symbols:
            continue
        checked += 1
        if symbol in allowed_symbols:
            supported += 1
            continue
        violations.append(
            {
                "type": "unsupported_contract_symbol",
                "claim": match.group(0),
                "evidence": "symbol is not present in EvidenceBundle facts or scope",
            }
        )
    lower_text = text.lower()
    for match in _STATUS_RE.finditer(lower_text):
        status = str(match.group(1) or "").strip()
        if not status or not allowed_statuses:
            continue
        segment = _segment_for_match(lower_text, match.start(), match.end())
        if not any(token in segment for token in _STATUS_CUE_TOKENS):
            continue
        checked += 1
        if status in allowed_statuses:
            supported += 1
            continue
        violations.append(
            {
                "type": "unsupported_contract_status",
                "claim": status,
                "evidence": "status is not present in EvidenceBundle status facts or scope",
            }
        )
    return AnswerVerificationResult(
        violations=tuple(violations[:8]),
        checked_claim_count=checked,
        supported_claim_count=supported,
    )


def _allowed_currency_amounts(evidence_bundle: EvidenceBundle) -> dict[str, list[float]]:
    allowed: dict[str, list[float]] = {}
    for fact in evidence_bundle.facts:
        currency = str(fact.currency or "").upper()
        if not currency or fact.unit != "currency":
            continue
        value = _parse_number(fact.value)
        if value is None:
            continue
        allowed.setdefault(currency, []).append(value)
    for calculation in evidence_bundle.calculations:
        if not isinstance(calculation, dict):
            continue
        for view in calculation.get("views") or []:
            if not isinstance(view, dict):
                continue
            if str(view.get("view") or "") not in {
                "cashflow",
                "net_income_cny",
                "assigned_stock_unrealized_pnl",
                "assigned_stock_realized_pnl",
                "assignment_lifecycle_pnl",
                "option_premium_attribution",
                "market_value",
                "cost_basis",
            }:
                continue
            sums = view.get("sums_by_currency")
            if not isinstance(sums, dict):
                continue
            for currency, raw_value in sums.items():
                value = _parse_number(raw_value)
                if value is not None:
                    allowed.setdefault(str(currency).upper(), []).append(value)
    return allowed


def _allowed_quantities(evidence_bundle: EvidenceBundle) -> dict[str, list[float]]:
    allowed: dict[str, list[float]] = {}
    for fact in evidence_bundle.facts:
        if fact.unit not in {"share", "contract"}:
            continue
        value = _parse_number(fact.value)
        if value is not None:
            allowed.setdefault(fact.unit, []).append(value)
    for dataset in evidence_bundle.datasets:
        value = _parse_number(dataset.get("row_count"))
        if value is not None:
            allowed.setdefault("row", []).append(value)
    return allowed


def _allowed_dates(evidence_bundle: EvidenceBundle) -> set[str]:
    allowed: set[str] = set()
    scope = evidence_bundle.scope if isinstance(evidence_bundle.scope, dict) else {}
    for month in scope.get("months") or []:
        normalized = _normalize_date(month)
        if normalized:
            allowed.add(normalized)
    time_range = scope.get("time_range") if isinstance(scope.get("time_range"), dict) else {}
    for key in ("start", "end"):
        normalized = _normalize_date(time_range.get(key))
        if normalized:
            allowed.add(normalized)
    for fact in evidence_bundle.facts:
        if fact.unit == "date":
            normalized = _normalize_date(fact.value)
            if normalized:
                allowed.add(normalized)
        normalized_as_of = _normalize_date(fact.as_of)
        if normalized_as_of:
            allowed.add(normalized_as_of)
    return allowed


def _allowed_symbols(evidence_bundle: EvidenceBundle) -> set[str]:
    allowed: set[str] = set()
    scope = evidence_bundle.scope if isinstance(evidence_bundle.scope, dict) else {}
    for symbol in scope.get("symbols") or []:
        normalized = _normalize_symbol(symbol)
        if normalized:
            allowed.add(normalized)
    for fact in evidence_bundle.facts:
        for value in (fact.symbol, fact.value if fact.unit == "symbol" else None):
            normalized = _normalize_symbol(value)
            if normalized:
                allowed.add(normalized)
    return allowed


def _allowed_statuses(evidence_bundle: EvidenceBundle) -> set[str]:
    allowed: set[str] = set()
    scope = evidence_bundle.scope if isinstance(evidence_bundle.scope, dict) else {}
    for status in scope.get("statuses") or []:
        normalized = _normalize_status(status)
        if normalized:
            allowed.add(normalized)
    for fact in evidence_bundle.facts:
        if fact.unit == "status":
            normalized = _normalize_status(fact.value)
            if normalized:
                allowed.add(normalized)
        if fact.freshness not in {"", "not_applicable"}:
            normalized_freshness = _normalize_status(fact.freshness)
            if normalized_freshness:
                allowed.add(normalized_freshness)
    for missing in evidence_bundle.missing_data:
        if isinstance(missing, dict):
            normalized = _normalize_status(missing.get("kind"))
            if normalized:
                allowed.add(normalized)
    return allowed


def _amount_supported(value: float, allowed_values: list[float], *, segment: str) -> bool:
    for allowed in allowed_values:
        tolerance = max(0.01, abs(allowed) * 0.00001)
        if abs(value - allowed) <= tolerance:
            return True
        if _approx_context(segment):
            approx_tolerance = max(10.0, abs(allowed) * 0.01)
            if abs(value - allowed) <= approx_tolerance:
                return True
        if value >= 0 and allowed < 0 and _loss_context(segment) and abs(value - abs(allowed)) <= tolerance:
            return True
    return False


def _number_supported(value: float, allowed_values: list[float]) -> bool:
    for allowed in allowed_values:
        tolerance = max(0.01, abs(allowed) * 0.00001)
        if abs(value - allowed) <= tolerance:
            return True
    return False


def _quantity_unit(label: str) -> str:
    if label == "股":
        return "share"
    if label == "张":
        return "contract"
    return "row"


def _normalize_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    text = raw.replace("/", "-")
    match_cn = re.fullmatch(r"(20\d{2})年(0?[1-9]|1[0-2])月(?:(0?[1-9]|[12]\d|3[01])日?)?", text)
    if match_cn:
        year = match_cn.group(1)
        month = int(match_cn.group(2))
        day = match_cn.group(3)
        if day:
            return f"{year}-{month:02d}-{int(day):02d}"
        return f"{year}-{month:02d}"
    match = re.fullmatch(r"(20\d{2})-(0?[1-9]|1[0-2])(?:-(0?[1-9]|[12]\d|3[01]))?", text)
    if not match:
        return None
    year = match.group(1)
    month = int(match.group(2))
    day = match.group(3)
    if day:
        return f"{year}-{month:02d}-{int(day):02d}"
    return f"{year}-{month:02d}"


def _normalize_symbol(value: Any) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    if raw.endswith(".HK"):
        base = raw[:-3]
        if base.isdigit():
            return f"{int(base):04d}.HK"
    return raw


def _normalize_status(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _approx_context(segment: str) -> bool:
    compact = str(segment or "").lower()
    return any(token in compact for token in _APPROX_TOKENS)


def _loss_context(segment: str) -> bool:
    compact = str(segment or "").lower()
    return any(token in compact for token in _LOSS_TOKENS)


def _parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _segment_for_match(text: str, start: int, end: int) -> str:
    left = max(text.rfind("\n", 0, start), text.rfind("。", 0, start), text.rfind("；", 0, start), text.rfind("，", 0, start))
    right_candidates = [
        index
        for index in (
            text.find("\n", end),
            text.find("。", end),
            text.find("；", end),
            text.find("，", end),
        )
        if index >= 0
    ]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right]


__all__ = [
    "AnswerVerificationResult",
    "verify_response_against_evidence",
]
