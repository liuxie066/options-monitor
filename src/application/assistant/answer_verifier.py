from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from src.application.assistant.evidence import EvidenceBundle


_CURRENCY_AMOUNT_RE = re.compile(r"\b(USD|HKD|CNY)\s*([-+]?\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)\s*[%％]")
_PERCENT_POINT_RE = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)\s*个?百分点")
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
    "HK",
    "LLM",
    "OM",
    "PNL",
    "US",
    "USD",
}
_STATUS_CUE_TOKENS = ("状态", "status", "quote", "行情", "预览", "确认", "取消", "写入")
_RATE_CUE_TOKENS = (
    "收益率",
    "回报率",
    "现金流率",
    "权利金率",
    "已实现率",
    "占比",
    "贡献率",
    "贡献",
    "比例",
    "百分点",
    "return rate",
    "returnrate",
    "rate",
    "yield",
    "share",
    "contribution",
    "percentage point",
)


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
    allowed_rates = _allowed_rate_values(evidence_bundle)
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
    for match in _PERCENT_RE.finditer(text):
        value = _parse_number(match.group(1))
        if value is None:
            continue
        segment = _segment_for_match(text, match.start(), match.end())
        if not _rate_context(segment):
            continue
        checked += 1
        if allowed_rates and _rate_supported(value / 100.0, allowed_rates, segment=segment):
            supported += 1
            continue
        violations.append(
            {
                "type": "unsupported_contract_rate",
                "claim": f"{match.group(1)}%",
                "evidence": "rate is not present in EvidenceBundle percent facts or derivable from same-row analysis numerator/denominator facts",
            }
        )
    for match in _PERCENT_POINT_RE.finditer(text):
        value = _parse_number(match.group(1))
        if value is None:
            continue
        segment = _segment_for_match(text, match.start(), match.end())
        if not _rate_context(segment):
            continue
        checked += 1
        if allowed_rates and _rate_supported(value / 100.0, allowed_rates, segment=segment):
            supported += 1
            continue
        violations.append(
            {
                "type": "unsupported_contract_rate",
                "claim": f"{match.group(1)}个百分点",
                "evidence": "rate difference is not present in EvidenceBundle percent facts or derived formula evidence",
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
    policy_violations, policy_checked, policy_supported = _semantic_policy_verification(text, evidence_bundle=evidence_bundle)
    checked += policy_checked
    supported += policy_supported
    violations.extend(policy_violations)
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
    for currency, values in _analysis_derived_currency_amounts(evidence_bundle).items():
        allowed.setdefault(currency, []).extend(values)
    for currency, values in _calculation_currency_amounts(evidence_bundle).items():
        allowed.setdefault(currency, []).extend(values)
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


def _allowed_rate_values(evidence_bundle: EvidenceBundle) -> list[float]:
    allowed: list[float] = []
    for fact in evidence_bundle.facts:
        if fact.unit != "percent":
            continue
        value = _parse_number(fact.value)
        if value is None:
            continue
        allowed.append(value)
        if abs(value) > 1 and abs(value) <= 100:
            allowed.append(value / 100.0)
    allowed.extend(_analysis_derived_rate_values(evidence_bundle))
    allowed.extend(_calculation_rate_values(evidence_bundle))
    return allowed


def _analysis_derived_rate_values(evidence_bundle: EvidenceBundle) -> list[float]:
    grouped: dict[tuple[str, str], dict[str, float]] = {}
    for fact in evidence_bundle.facts:
        if fact.source_tool != "analysis_query":
            continue
        value = _parse_number(fact.value)
        if value is None:
            continue
        row_key = _fact_row_key(fact)
        if not row_key:
            continue
        field_name = _fact_field_name(fact)
        if not field_name:
            continue
        key = (row_key, str(fact.source_label or ""))
        grouped.setdefault(key, {})[field_name] = float(value)

    values: list[float] = []
    for row in grouped.values():
        cash_secured = row.get("cash_secured_cny")
        if cash_secured is None or abs(cash_secured) < 0.000001:
            continue
        for numerator_name in ("net_income_cny", "premium_income_cny", "realized_pnl_cny"):
            numerator = row.get(numerator_name)
            if numerator is None:
                continue
            values.append(round(numerator / cash_secured, 8))
    return values


def _analysis_derived_currency_amounts(evidence_bundle: EvidenceBundle) -> dict[str, list[float]]:
    grouped: dict[tuple[str, str, str], list[float]] = {}
    for fact in evidence_bundle.facts:
        if fact.source_tool != "analysis_query" or fact.unit != "currency" or not fact.currency:
            continue
        if not _is_difference_source_fact(fact):
            continue
        amount = _parse_number(fact.value)
        if amount is None:
            continue
        row_key = _fact_row_key(fact)
        if not row_key:
            continue
        key = (row_key, str(fact.currency).upper(), str(fact.source_label or ""))
        bucket = grouped.setdefault(key, [])
        if len(bucket) < 12:
            bucket.append(float(amount))
    derived: dict[str, list[float]] = {}
    for (_row_key, currency, _source_label), values in grouped.items():
        if len(values) < 2:
            continue
        out = derived.setdefault(currency, [])
        for left_index, left in enumerate(values):
            for right in values[left_index + 1 :]:
                diff = round(float(left) - float(right), 6)
                if abs(diff) < 0.000001:
                    continue
                out.extend([diff, -diff, abs(diff)])
    return derived


def _calculation_currency_amounts(evidence_bundle: EvidenceBundle) -> dict[str, list[float]]:
    allowed: dict[str, list[float]] = {}
    for formula in _calculation_formulas(evidence_bundle):
        if str(formula.get("kind") or "") not in {"amount_difference", "amount_sum", "assigned_stock_lifecycle"}:
            continue
        currency = str(formula.get("currency") or "").upper()
        if not currency:
            continue
        values = formula.get("values")
        if not isinstance(values, list):
            values = [formula.get("value")]
        for raw_value in values:
            value = _parse_number(raw_value)
            if value is not None:
                allowed.setdefault(currency, []).append(value)
    return allowed


def _calculation_rate_values(evidence_bundle: EvidenceBundle) -> list[float]:
    allowed: list[float] = []
    for formula in _calculation_formulas(evidence_bundle):
        if str(formula.get("kind") or "") not in {"ratio", "rate_difference", "contribution_share"}:
            continue
        values = formula.get("values")
        if not isinstance(values, list):
            values = [formula.get("value")]
        for raw_value in values:
            value = _parse_number(raw_value)
            if value is not None:
                allowed.append(_normalize_rate_value(value))
    return allowed


def _calculation_formulas(evidence_bundle: EvidenceBundle) -> list[dict[str, Any]]:
    formulas: list[dict[str, Any]] = []
    for calculation in evidence_bundle.calculations:
        if not isinstance(calculation, dict):
            continue
        for formula in calculation.get("formulas") or []:
            if isinstance(formula, dict):
                formulas.append(formula)
    return formulas


def _fact_row_key(fact: Any) -> str:
    source_path = str(getattr(fact, "source_path", "") or "")
    match = re.match(r"rows\[(\d+)\]\.", source_path)
    if match:
        return f"rows[{match.group(1)}]"
    return ""


def _is_difference_source_fact(fact: Any) -> bool:
    field_name = _fact_field_name(fact)
    if any(token in field_name for token in ("diff", "difference", "delta")):
        return False
    if any(token in field_name for token in ("spot", "strike", "price", "per_share")):
        return False
    return any(
        token in field_name
        for token in (
            "income",
            "pnl",
            "cashflow",
            "premium",
            "amount",
            "market_value",
            "cost_basis",
            "basis",
            "gross",
        )
    )


def _fact_field_name(fact: Any) -> str:
    source_path = str(getattr(fact, "source_path", "") or "")
    path = str(getattr(fact, "path", "") or "")
    return (source_path.rsplit(".", 1)[-1] or path.rsplit(".", 1)[-1]).lower()


def _semantic_policy_verification(text: str, *, evidence_bundle: EvidenceBundle) -> tuple[list[dict[str, Any]], int, int]:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    violations: list[dict[str, Any]] = []
    checked = 0
    supported = 0

    if _claims_all_accounts(compact) and _has_analysis_coverage(evidence_bundle):
        checked += 1
        if _has_all_account_caveat(compact) or _all_account_claim_supported(evidence_bundle):
            supported += 1
        else:
            violations.append(
                {
                    "type": "unsupported_analysis_coverage_all_accounts",
                    "claim": "全部账户",
                    "evidence": "analysis evidence coverage is filtered or does not prove all configured accounts",
                }
            )

    if _claims_fresh_or_current(compact) and _has_analysis_freshness(evidence_bundle):
        checked += 1
        bad_freshness = _bad_freshness_records(evidence_bundle)
        if not bad_freshness or _has_freshness_caveat(compact):
            supported += 1
        else:
            violations.append(
                {
                    "type": "unsupported_analysis_freshness_claim",
                    "claim": "最新/实时/当前",
                    "evidence": "analysis evidence contains stale, missing, or unknown freshness records",
                    "freshness": bad_freshness[:4],
                }
            )

    if _claims_average_return_rate(compact) and _has_rate_aggregation_warning(evidence_bundle):
        checked += 1
        violations.append(
            {
                "type": "unsupported_analysis_rate_aggregation",
                "claim": "平均收益率",
                "evidence": "analysis evidence marks return-rate aggregation as invalid or warning",
            }
        )

    if _claims_root_cause(compact) and _has_summary_only_analysis_coverage(evidence_bundle) and not _has_cause_caveat(compact):
        checked += 1
        violations.append(
            {
                "type": "unsupported_analysis_root_cause_claim",
                "claim": "原因/主要来自",
                "evidence": "analysis evidence only covers account-level summary views, not component or symbol-level drivers",
            }
        )

    if _claims_root_cause(compact) and _has_unresolved_analysis_diagnostics(evidence_bundle) and not _has_cause_caveat(compact):
        checked += 1
        violations.append(
            {
                "type": "unsupported_analysis_diagnostic_root_cause_claim",
                "claim": "原因/根因",
                "evidence": "analysis diagnostic evidence is missing, empty, unreadable, or has no matching rows",
                "diagnostics": _unresolved_analysis_diagnostics(evidence_bundle)[:4],
            }
        )

    return violations, checked, supported


def _analysis_evidence_records(evidence_bundle: EvidenceBundle) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for dataset in evidence_bundle.datasets:
        analysis_evidence = dataset.get("analysis_evidence") if isinstance(dataset.get("analysis_evidence"), dict) else None
        if isinstance(analysis_evidence, dict):
            records.append(analysis_evidence)
    return records


def _analysis_diagnostic_records(evidence_bundle: EvidenceBundle) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for evidence in _analysis_evidence_records(evidence_bundle):
        diagnostics = evidence.get("diagnostics")
        if not isinstance(diagnostics, list):
            continue
        records.extend(dict(item) for item in diagnostics if isinstance(item, dict))
    return records


def _unresolved_analysis_diagnostics(evidence_bundle: EvidenceBundle) -> list[dict[str, Any]]:
    unresolved_statuses = {
        "diagnostic_missing",
        "no_matching_rows",
        "read_error",
        "empty_artifact",
    }
    rows: list[dict[str, Any]] = []
    for record in _analysis_diagnostic_records(evidence_bundle):
        status = str(record.get("status") or "").strip().lower()
        if status not in unresolved_statuses:
            continue
        rows.append(
            {
                key: value
                for key, value in record.items()
                if key in {"view", "status", "severity", "summary", "answer_boundary"}
            }
        )
    return rows


def _has_unresolved_analysis_diagnostics(evidence_bundle: EvidenceBundle) -> bool:
    return bool(_unresolved_analysis_diagnostics(evidence_bundle))


def _has_analysis_coverage(evidence_bundle: EvidenceBundle) -> bool:
    return any(isinstance(record.get("coverage"), dict) for record in _analysis_evidence_records(evidence_bundle))


def _coverage_accounts(evidence_bundle: EvidenceBundle) -> set[str]:
    accounts: set[str] = set()
    for record in _analysis_evidence_records(evidence_bundle):
        coverage = record.get("coverage") if isinstance(record.get("coverage"), dict) else {}
        for account in coverage.get("accounts") or []:
            text = str(account or "").strip()
            if text:
                accounts.add(text)
    return accounts


def _coverage_views(evidence_bundle: EvidenceBundle) -> set[str]:
    views: set[str] = set()
    for record in _analysis_evidence_records(evidence_bundle):
        coverage = record.get("coverage") if isinstance(record.get("coverage"), dict) else {}
        for view in coverage.get("views") or []:
            text = str(view or "").strip()
            if text:
                views.add(text)
    return views


def _all_account_claim_supported(evidence_bundle: EvidenceBundle) -> bool:
    accounts = _coverage_accounts(evidence_bundle)
    if len(accounts) <= 1:
        return False
    for dataset in evidence_bundle.datasets:
        if str(dataset.get("tool_name") or "") != "analysis_query":
            continue
        payload = dataset.get("payload") if isinstance(dataset.get("payload"), dict) else {}
        account_arg = str(payload.get("account") or payload.get("accounts") or "").strip().lower()
        if account_arg and account_arg not in {"all", "*", "全部", "全部账户"}:
            return False
        sql = re.sub(r"\s+", " ", str(payload.get("sql") or payload.get("query") or "").lower())
        if re.search(r"\baccount\s*(?:=|in)\b", sql):
            return False
    return True


def _has_analysis_freshness(evidence_bundle: EvidenceBundle) -> bool:
    return any(record.get("freshness") for record in _analysis_evidence_records(evidence_bundle))


def _bad_freshness_records(evidence_bundle: EvidenceBundle) -> list[dict[str, Any]]:
    bad: list[dict[str, Any]] = []
    bad_statuses = {"missing", "missing_quote", "stale", "unknown", "error", "failed"}
    for record in _analysis_evidence_records(evidence_bundle):
        freshness_rows = record.get("freshness")
        if not isinstance(freshness_rows, list):
            continue
        for freshness in freshness_rows:
            if not isinstance(freshness, dict):
                continue
            status = str(
                freshness.get("freshness")
                or freshness.get("status")
                or freshness.get("quote_status")
                or ""
            ).strip().lower()
            if status in bad_statuses:
                bad.append({key: value for key, value in freshness.items() if key in {"view", "source", "symbol", "freshness", "status", "quote_status"}})
    return bad


def _has_rate_aggregation_warning(evidence_bundle: EvidenceBundle) -> bool:
    for record in _analysis_evidence_records(evidence_bundle):
        policies = record.get("aggregation_policy")
        if not isinstance(policies, list):
            continue
        for policy in policies:
            if not isinstance(policy, dict):
                continue
            field = str(policy.get("field") or "").lower()
            status = str(policy.get("status") or "").lower()
            policy_name = str(policy.get("policy") or "").lower()
            if "rate" in field and (status in {"warning", "invalid", "error"} or "invalid" in policy_name):
                return True
    return False


def _has_summary_only_analysis_coverage(evidence_bundle: EvidenceBundle) -> bool:
    views = _coverage_views(evidence_bundle)
    if not views:
        return False
    detail_views = {"account_monthly_income_components", "symbol_income_attribution"}
    summary_views = {"account_monthly_performance", "monthly_income_return_summary", "monthly_income_combined_return_summary"}
    return bool(views & summary_views) and not bool(views & detail_views)


def _claims_all_accounts(compact: str) -> bool:
    return any(token in compact for token in ("全部账户", "所有账户", "全账户", "allaccounts"))


def _has_all_account_caveat(compact: str) -> bool:
    return any(token in compact for token in ("不是全部账户", "非全部账户", "不代表全部账户", "只覆盖", "仅覆盖", "notallaccounts", "partial"))


def _claims_fresh_or_current(compact: str) -> bool:
    return any(token in compact for token in ("最新", "实时", "当前", "现在", "latest", "realtime", "real-time", "current"))


def _has_freshness_caveat(compact: str) -> bool:
    return any(token in compact for token in ("缺失", "缺少", "missing", "stale", "过期", "未知", "unknown", "无法", "不能确认", "快照", "snapshot", "非实时"))


def _claims_average_return_rate(compact: str) -> bool:
    return any(token in compact for token in ("平均收益率", "平均回报率", "averagereturnrate", "avgreturnrate", "avg(net_return_rate)"))


def _claims_root_cause(compact: str) -> bool:
    return any(token in compact for token in ("主要来自", "主要原因", "原因是", "根因", "rootcause", "driver", "drivers", "sourceof"))


def _has_cause_caveat(compact: str) -> bool:
    return any(
        token in compact
        for token in (
            "无法确认",
            "无法判断",
            "不能确认",
            "证据不足",
            "无匹配",
            "没有匹配",
            "需要明细",
            "缺少明细",
            "缺少",
            "可能",
            "推测",
            "insufficient",
            "unknown",
        )
    )


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


def _rate_supported(value: float, allowed_values: list[float], *, segment: str) -> bool:
    for allowed in allowed_values:
        tolerance = max(0.0001, abs(allowed) * 0.001)
        if abs(value - allowed) <= tolerance:
            return True
        if _approx_context(segment):
            approx_tolerance = max(0.001, abs(allowed) * 0.02)
            if abs(value - allowed) <= approx_tolerance:
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


def _rate_context(segment: str) -> bool:
    compact = str(segment or "").lower()
    return any(token in compact for token in _RATE_CUE_TOKENS)


def _parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _normalize_rate_value(value: float) -> float:
    return value / 100.0 if abs(value) > 1 and abs(value) <= 100 else value


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
