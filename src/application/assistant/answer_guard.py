from __future__ import annotations

import re
from typing import Any

from src.application.assistant.answer_verifier import verify_response_against_evidence, verify_response_shape


def verify_answer_guard(
    response_text: str,
    *,
    observations: list[dict[str, Any]],
    evidence_bundle: Any | None = None,
    task_contract: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(response_text or "")
    compact = re.sub(r"\s+", "", text.lower())
    facts = _answer_guard_facts(observations)
    violations: list[dict[str, Any]] = []
    violations.extend(_normal_answer_ux_violations(text))
    if facts["all_tools_ok"] and any(token in compact for token in ("工具查询失败", "工具调用失败", "查询失败")):
        violations.append(
            {
                "type": "contradicts_tool_status",
                "claim": "工具查询失败",
                "evidence": "all observed tools returned ok=true",
            }
        )
    if facts["complete_for_query_scope"]:
        missing_scope_tokens = (
            "无法直接确认",
            "无法确认",
            "不能确认",
            "无法提供完整",
            "需要提供完整",
            "请提供完整",
            "缺少所有月份",
            "缺少完整月份",
            "缺少所有账户",
        )
        if any(token in compact for token in missing_scope_tokens):
            violations.append(
                {
                    "type": "contradicts_query_coverage",
                    "claim": "缺少查询范围内的数据",
                    "evidence": f"coverage is complete for query scope; months={facts['months']}; accounts={facts['accounts']}",
                }
            )
    for account in facts["accounts"]:
        account_text = str(account).lower()
        if not account_text:
            continue
        if re.search(rf"(未包含|不包含|缺少|没有|未提供)[^，。；;\n]{{0,12}}{re.escape(account_text)}", compact) or re.search(
            rf"{re.escape(account_text)}[^，。；;\n]{{0,12}}(未包含|不包含|缺少|没有|未提供)",
            compact,
        ):
            violations.append(
                {
                    "type": "contradicts_account_coverage",
                    "claim": f"缺少账户 {account}",
                    "evidence": f"coverage.accounts includes {account}",
                }
            )
    for month in facts["months"]:
        month_text = str(month)
        month_cn = _month_label_cn(month_text)
        month_tokens = [month_text, month_text.replace("-", "年") + "月", month_cn]
        for token in month_tokens:
            if not token:
                continue
            normalized = token.lower()
            if re.search(rf"(未提供|缺少|没有)[^，。；;\n]{{0,12}}{re.escape(normalized)}", compact) or re.search(
                rf"{re.escape(normalized)}[^，。；;\n]{{0,12}}(未提供|缺少|没有|数据缺失)",
                compact,
            ):
                violations.append(
                    {
                        "type": "contradicts_month_coverage",
                        "claim": f"缺少月份 {month}",
                        "evidence": f"coverage.months includes {month}",
                    }
                )
                break
    if facts["cashflow_row_count"] > 0 and any(token in compact for token in ("没有明细", "明细为空", "无明细")):
        violations.append(
            {
                "type": "contradicts_detail_rows",
                "claim": "没有明细",
                "evidence": f"cashflow_row_count={facts['cashflow_row_count']}",
            }
        )
    for fact in facts["contract_facts"]:
        expected_contracts = _safe_float(fact.get("contracts"))
        if expected_contracts is None or expected_contracts == 1:
            continue
        for segment in _answer_guard_segments(text):
            if not _segment_matches_contract_fact(segment, fact):
                continue
            if _contains_singular_contract_claim(segment):
                violations.append(
                    {
                        "type": "contradicts_contract_quantity",
                        "claim": "一手/1张",
                        "evidence": (
                            f"{fact.get('account') or '-'} {fact.get('symbol') or '-'} "
                            f"{fact.get('option_type') or '-'} expected contracts={_format_contract_count(expected_contracts)} "
                            f"from {fact.get('row_type') or 'monthly_income_report'}."
                        ),
                    }
                )
                break
    violations.extend(_unsupported_assigned_stock_numeric_claims(text, observations=observations))
    contract_verification = verify_response_against_evidence(text, evidence_bundle=evidence_bundle)
    violations.extend(contract_verification.violations)
    shape_verification = verify_response_shape(text, task_contract=task_contract, coverage=coverage)
    violations.extend(shape_verification.violations)
    return {
        "facts": facts,
        "contract_verifier": contract_verification.public_payload(),
        "shape_verifier": shape_verification.public_payload(),
        "violations": violations,
    }


def answer_guard_trace_payload(status: str, guard: dict[str, Any]) -> dict[str, Any]:
    violations = [dict(item) for item in guard.get("violations") or [] if isinstance(item, dict)]
    retry_violations = [dict(item) for item in guard.get("retry_violations") or [] if isinstance(item, dict)]
    contract_verifier = dict(guard.get("contract_verifier") or {}) if isinstance(guard.get("contract_verifier"), dict) else {}
    retry_contract_verifier = (
        dict(guard.get("retry_contract_verifier") or {}) if isinstance(guard.get("retry_contract_verifier"), dict) else {}
    )
    shape_verifier = dict(guard.get("shape_verifier") or {}) if isinstance(guard.get("shape_verifier"), dict) else {}
    retry_shape_verifier = dict(guard.get("retry_shape_verifier") or {}) if isinstance(guard.get("retry_shape_verifier"), dict) else {}
    out: dict[str, Any] = {
        "status": status,
        "violations": violations,
        "violation_type": str(violations[0].get("type") or "") if violations else None,
        "violation_types": sorted({str(item.get("type") or "") for item in violations if item.get("type")}),
    }
    if retry_violations:
        out["retry_violations"] = retry_violations
        out["retry_violation_type"] = str(retry_violations[0].get("type") or "")
        out["retry_violation_types"] = sorted({str(item.get("type") or "") for item in retry_violations if item.get("type")})
    if contract_verifier:
        out["contract_verifier"] = contract_verifier
        out["claim_classification"] = list(contract_verifier.get("claim_classification") or [])
    if retry_contract_verifier:
        out["retry_contract_verifier"] = retry_contract_verifier
        out["retry_claim_classification"] = list(retry_contract_verifier.get("claim_classification") or [])
    if shape_verifier:
        out["shape_verifier"] = shape_verifier
    if retry_shape_verifier:
        out["retry_shape_verifier"] = retry_shape_verifier
    return out


_UX_FORCED_SECTION_RE = re.compile(r"(?im)^\s*(?:事实|分析)\s*[:：]?\s*$")
_UX_INTERNAL_MODE_RE = re.compile(
    r"(?i)\b(?:canonical|synthesis|fact\s*mode|analysis\s*mode|tool_plan|output_contract|evidencebundle|assistant\.answer_evidence)\b"
)
_UX_TOOL_NAME_RE = re.compile(r"(?i)\b(?:analysis_query|analysis_catalog)\b")
_UX_SQL_RE = re.compile(r"(?is)(?:\bsql\b|\bselect\b.{0,240}\bfrom\b|\bwith\b.{0,240}\bselect\b)")
_UX_INTERNAL_ID_RE = re.compile(r"(?i)\b(?:stock_lot_id|record_id|event_id|source_deal_id|position_key|trace_id|artifact_path)\b")
_UX_INTERNAL_PATH_RE = re.compile(r"(?i)(?:/Volumes/|/Users/|output_runs/|output_shared/|candidate_filter_trace\.jsonl|\.(?:sqlite3|jsonl)\b)")


def _normal_answer_ux_violations(response_text: str) -> list[dict[str, Any]]:
    text = str(response_text or "")
    checks: tuple[tuple[str, str, re.Pattern[str]], ...] = (
        ("unsupported_internal_mode_leak", "internal answer mode leaked", _UX_INTERNAL_MODE_RE),
        ("unsupported_internal_tool_leak", "internal tool name leaked", _UX_TOOL_NAME_RE),
        ("unsupported_internal_sql_leak", "SQL detail leaked", _UX_SQL_RE),
        ("unsupported_internal_id_leak", "internal identifier leaked", _UX_INTERNAL_ID_RE),
        ("unsupported_internal_path_leak", "internal artifact path leaked", _UX_INTERNAL_PATH_RE),
        ("unsupported_forced_fact_analysis_split", "forced fact/analysis section split leaked", _UX_FORCED_SECTION_RE),
    )
    violations: list[dict[str, Any]] = []
    for violation_type, evidence, pattern in checks:
        match = pattern.search(text)
        if not match:
            continue
        claim = str(match.group(0) or "").strip()
        violations.append(
            {
                "type": violation_type,
                "claim": claim[:80],
                "evidence": evidence,
            }
        )
    return violations


def with_answer_guard_feedback(observations: list[dict[str, Any]], guard: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *observations,
        {
            "index": len(observations) + 1,
            "tool_name": "assistant.answer_guard",
            "payload": {},
            "ok": True,
            "error": None,
            "data": {
                "violations": guard.get("violations") or [],
                "rewrite_instruction": (
                    "Your previous response contradicted tool observations. Rewrite using only observations. "
                    "Also satisfy the required answer shape from any answer_shape violations: include named accounts, "
                    "differences, rates, drivers, freshness, or explicit missing-data impact when required. "
                    "When monthly_income_report query_scope.month=all_available, answer over the OM local ledger coverage. "
                    "Do not claim missing months/accounts unless coverage or diagnostics explicitly says so. "
                    "Answer as one natural user-facing Agent response; do not expose canonical/synthesis/fact/analysis modes, "
                    "SQL, tool names, internal ids, artifact paths, or raw tool receipts. "
                    "For factual rows, use contracts/contracts_open/contracts_closed as the trade quantity; "
                    "do not treat one row or one lot as one contract, and do not alter symbols, dates, strikes, or accounts."
                ),
            },
        },
    ]


def _answer_guard_facts(observations: list[dict[str, Any]]) -> dict[str, Any]:
    months: set[str] = set()
    accounts: set[str] = set()
    all_tools_ok = True
    complete_for_query_scope = False
    cashflow_row_count = 0
    contract_facts: list[dict[str, Any]] = []
    for item in observations:
        if not bool(item.get("ok", False)):
            all_tools_ok = False
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if not isinstance(data, dict):
            continue
        contract = item.get("output_contract") if isinstance(item.get("output_contract"), dict) else {}
        guard_profile = str(contract.get("guard_profile") or "").strip()
        coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
        for month in coverage.get("months") or []:
            if str(month).strip():
                months.add(str(month))
        for account in coverage.get("accounts") or []:
            if str(account).strip():
                accounts.add(str(account))
        if bool(coverage.get("complete_for_query_scope")):
            complete_for_query_scope = True
        count = data.get("cashflow_row_count")
        if count is None:
            rows = data.get("cashflow_rows")
            count = len(rows) if isinstance(rows, list) else 0
        try:
            cashflow_row_count = max(cashflow_row_count, int(count or 0))
        except Exception:
            pass
        if guard_profile in {"income_summary", "income_rows"} or str(item.get("tool_name") or "") == "monthly_income_report":
            contract_facts.extend(_monthly_income_contract_facts(data))
        if guard_profile == "position_rows" or str(item.get("tool_name") or "") == "option_positions_read":
            contract_facts.extend(_position_contract_facts(data))
    return {
        "months": sorted(months),
        "accounts": sorted(accounts),
        "all_tools_ok": all_tools_ok,
        "complete_for_query_scope": complete_for_query_scope,
        "cashflow_row_count": cashflow_row_count,
        "contract_facts": contract_facts,
    }


def _monthly_income_contract_facts(data: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for row_type, rows_key, quantity_key in (
        ("cashflow_rows", "cashflow_rows", "contracts"),
        ("premium_rows", "premium_rows", "contracts"),
        ("realized_rows", "realized_rows", "contracts_closed"),
    ):
        rows = data.get(rows_key)
        if not isinstance(rows, list):
            continue
        for row_raw in rows:
            if not isinstance(row_raw, dict):
                continue
            contracts = _safe_float(row_raw.get(quantity_key))
            if contracts is None or contracts <= 0:
                continue
            facts.append(
                {
                    "row_type": row_type,
                    "quantity_field": quantity_key,
                    "contracts": contracts,
                    "month": row_raw.get("month"),
                    "account": row_raw.get("account"),
                    "symbol": row_raw.get("symbol"),
                    "option_type": row_raw.get("option_type"),
                    "trade_action": row_raw.get("trade_action"),
                    "close_type": row_raw.get("close_type"),
                    "realized_gross": row_raw.get("realized_gross"),
                    "net_cashflow_gross": row_raw.get("net_cashflow_gross"),
                    "premium_received_gross": row_raw.get("premium_received_gross"),
                    "currency": row_raw.get("currency"),
                }
            )
    return facts


def _position_contract_facts(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("rows")
    if not isinstance(rows, list):
        rows = data.get("positions")
    if not isinstance(rows, list):
        return []
    facts: list[dict[str, Any]] = []
    for row_raw in rows:
        if not isinstance(row_raw, dict):
            continue
        contracts = _safe_float(row_raw.get("contracts_open") if row_raw.get("contracts_open") is not None else row_raw.get("contracts"))
        if contracts is None or contracts <= 0:
            continue
        facts.append(
            {
                "row_type": "option_positions_read.rows",
                "quantity_field": "contracts_open",
                "contracts": contracts,
                "account": row_raw.get("account"),
                "symbol": row_raw.get("symbol"),
                "option_type": row_raw.get("option_type"),
                "side": row_raw.get("side"),
                "strike": row_raw.get("strike"),
                "expiration_ymd": row_raw.get("expiration_ymd"),
                "expiration": row_raw.get("expiration"),
            }
        )
    return facts


_CURRENCY_NUMERIC_CLAIM_RE = re.compile(r"\b(?:USD|HKD|CNY)\s*([-+]?\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)
_UNIT_NUMERIC_CLAIM_RE = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)\s*(股|条|笔|张)")
_PERCENT_NUMERIC_CLAIM_RE = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)\s*%")


def _unsupported_assigned_stock_numeric_claims(
    response_text: str,
    *,
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed_values = _assigned_stock_allowed_numeric_values(observations)
    if not allowed_values:
        return []
    violations: list[dict[str, Any]] = []
    for raw in _CURRENCY_NUMERIC_CLAIM_RE.findall(response_text):
        value = _parse_claim_number(raw)
        if value is not None and not _numeric_value_allowed(value, allowed_values):
            violations.append(
                {
                    "type": "unsupported_assigned_stock_number",
                    "claim": raw,
                    "evidence": "number is not present in assigned-stock tool rows or currency totals",
                }
            )
    for raw, unit in _UNIT_NUMERIC_CLAIM_RE.findall(response_text):
        value = _parse_claim_number(raw)
        if value is not None and not _numeric_value_allowed(value, allowed_values):
            violations.append(
                {
                    "type": "unsupported_assigned_stock_number",
                    "claim": f"{raw}{unit}",
                    "evidence": "number is not present in assigned-stock tool rows, counts, or share quantities",
                }
            )
    for raw in _PERCENT_NUMERIC_CLAIM_RE.findall(response_text):
        violations.append(
            {
                "type": "unsupported_assigned_stock_percent",
                "claim": f"{raw}%",
                "evidence": "assigned-stock tool output does not provide percentage return facts",
            }
        )
    return violations[:5]


def _assigned_stock_allowed_numeric_values(observations: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    numeric_fields = (
        "shares_remaining",
        "shares_sold",
        "stock_cost_per_share",
        "remaining_stock_cost_basis",
        "remaining_market_value",
        "spot",
        "assigned_stock_unrealized_pnl",
        "assigned_stock_realized_pnl",
        "option_premium_attribution",
        "assignment_lifecycle_pnl",
    )
    summed_fields = (
        "remaining_stock_cost_basis",
        "remaining_market_value",
        "assigned_stock_unrealized_pnl",
        "assigned_stock_realized_pnl",
        "option_premium_attribution",
        "assignment_lifecycle_pnl",
    )
    for item in observations:
        if str(item.get("tool_name") or "") != "option_positions_read":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if str(data.get("action") or "").strip().lower() != "assigned-stock":
            contract = item.get("output_contract") if isinstance(item.get("output_contract"), dict) else {}
            if str(contract.get("canonical_renderer") or "") != "assigned_stock_lifecycle":
                continue
        rows = data.get("rows") or data.get("assigned_stock_lots")
        if not isinstance(rows, list):
            continue
        _append_numeric_value(values, data.get("row_count"))
        _append_numeric_value(values, len(rows))
        counts_by_symbol: dict[str, int] = {}
        counts_by_currency: dict[str, int] = {}
        sums_by_currency: dict[str, dict[str, float]] = {}
        for row_raw in rows:
            if not isinstance(row_raw, dict):
                continue
            symbol = str(row_raw.get("symbol") or "").strip()
            currency = str(row_raw.get("currency") or "").strip().upper()
            if symbol:
                counts_by_symbol[symbol] = counts_by_symbol.get(symbol, 0) + 1
            if currency:
                counts_by_currency[currency] = counts_by_currency.get(currency, 0) + 1
            for field in numeric_fields:
                _append_numeric_value(values, row_raw.get(field))
            bucket = sums_by_currency.setdefault(currency, {})
            for field in summed_fields:
                amount = _safe_float(row_raw.get(field))
                if amount is not None:
                    bucket[field] = bucket.get(field, 0.0) + amount
        for count in [*counts_by_symbol.values(), *counts_by_currency.values()]:
            _append_numeric_value(values, count)
        for bucket in sums_by_currency.values():
            for amount in bucket.values():
                _append_numeric_value(values, amount)
    return values


def _append_numeric_value(values: list[float], value: Any) -> None:
    number = _safe_float(value)
    if number is not None:
        values.append(number)


def _parse_claim_number(raw: str) -> float | None:
    try:
        return float(str(raw or "").replace(",", ""))
    except Exception:
        return None


def _numeric_value_allowed(value: float, allowed_values: list[float]) -> bool:
    for allowed in allowed_values:
        tolerance = max(0.01, abs(allowed) * 0.00001)
        if abs(value - allowed) <= tolerance:
            return True
    return False


def _answer_guard_segments(text: str) -> list[str]:
    return [segment for segment in re.split(r"[\n。！？!?]+", str(text or "")) if segment.strip()]


def _segment_matches_contract_fact(segment: str, fact: dict[str, Any]) -> bool:
    compact = re.sub(r"\s+", "", str(segment or "").lower())
    if not compact:
        return False
    account = str(fact.get("account") or "").strip().lower()
    if account and account not in compact:
        return False
    symbol = str(fact.get("symbol") or "").strip().lower()
    if symbol:
        symbol_tokens = {symbol, symbol.replace(".", "")}
        if "." in symbol:
            symbol_tokens.add(symbol.split(".", 1)[0])
        if not any(token and token in compact for token in symbol_tokens):
            return False
    option_type = str(fact.get("option_type") or "").strip().lower()
    if option_type == "put" and not any(token in compact for token in ("put", "沽", "认沽")):
        return False
    if option_type == "call" and not any(token in compact for token in ("call", "购", "认购")):
        return False
    return True


def _contains_singular_contract_claim(segment: str) -> bool:
    compact = re.sub(r"\s+", "", str(segment or "").lower())
    return bool(re.search(r"(^|[^\d])1(手|张)", compact) or "一手" in compact or "一张" in compact)


def _format_contract_count(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _month_label_cn(month: str) -> str:
    parts = str(month or "").split("-")
    if len(parts) != 2:
        return ""
    try:
        return f"{int(parts[1])}月"
    except Exception:
        return ""


__all__ = [
    "answer_guard_trace_payload",
    "verify_answer_guard",
    "with_answer_guard_feedback",
]
