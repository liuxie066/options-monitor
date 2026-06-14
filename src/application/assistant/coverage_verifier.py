from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from src.application.assistant.evidence import EvidenceBundle
from src.application.assistant.task_contract import TaskContract


COVERAGE_RESULT_SCHEMA_VERSION = "om-agent-coverage-v1"


@dataclass(frozen=True)
class CoverageResult:
    status: str
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    gaps: tuple[dict[str, Any], ...] = ()
    next_action: str = "final_answer"
    schema_version: str = COVERAGE_RESULT_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "satisfied": list(self.satisfied),
            "missing": list(self.missing),
            "gaps": [dict(item) for item in self.gaps],
            "next_action": self.next_action,
        }


def verify_coverage(*, task_contract: TaskContract, evidence_bundle: EvidenceBundle) -> CoverageResult:
    evidence = evidence_bundle.public_payload()
    facts = [item for item in evidence.get("facts") or [] if isinstance(item, dict)]
    datasets = [item for item in evidence.get("datasets") or [] if isinstance(item, dict)]
    diagnostics = [item for item in evidence.get("diagnostics") or [] if isinstance(item, dict)]
    missing_data = [item for item in evidence.get("missing_data") or [] if isinstance(item, dict)]
    provided = _provided_answer_keys(facts=facts, datasets=datasets, diagnostics=diagnostics, task_contract=task_contract)
    gaps: list[dict[str, Any]] = []
    gaps.extend(_account_coverage_gaps(task_contract=task_contract, evidence=evidence, facts=facts, datasets=datasets))
    gaps.extend(_account_comparison_metric_gaps(task_contract=task_contract, evidence=evidence, facts=facts, datasets=datasets))
    gaps.extend(_breakdown_gaps(task_contract=task_contract, datasets=datasets, facts=facts))
    gaps.extend(_assigned_stock_quote_gaps(task_contract=task_contract, missing_data=missing_data, facts=facts))
    gaps.extend(
        _upgrade_status_gaps(
            task_contract=task_contract,
            provided=provided,
            diagnostics=diagnostics,
            missing_data=missing_data,
            datasets=datasets,
        )
    )
    satisfied = [key for key in task_contract.required_answer if key in provided or _key_satisfied_by_scope(key, task_contract, evidence)]
    missing = [key for key in task_contract.required_answer if key not in satisfied]
    if gaps:
        deduped_gaps = _dedupe_gaps(gaps)
        recoverable = any(_gap_is_recoverable(gap) for gap in deduped_gaps)
        return CoverageResult(
            status="recoverable_gap" if recoverable else "unrecoverable_gap",
            satisfied=tuple(satisfied),
            missing=tuple(missing),
            gaps=tuple(deduped_gaps),
            next_action="followup_tool" if recoverable else "answer_with_missing_data",
        )
    if missing:
        return CoverageResult(
            status="unrecoverable_gap",
            satisfied=tuple(satisfied),
            missing=tuple(missing),
            next_action="answer_with_missing_data",
        )
    return CoverageResult(status="complete", satisfied=tuple(satisfied), missing=())


def _provided_answer_keys(
    *,
    facts: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    task_contract: TaskContract,
) -> set[str]:
    keys: set[str] = set()
    if any(str(item.get("source_label") or "").strip() for item in datasets):
        keys.add("source_and_policy")
    requested_accounts = _requested_accounts(task_contract)
    amount_groups = _comparable_account_metric_groups(facts, requested_accounts=requested_accounts, rate=False)
    rate_groups = _comparable_account_metric_groups(facts, requested_accounts=requested_accounts, rate=True)
    if amount_groups:
        keys.update({"comparison_winner", "amount_difference"})
    if rate_groups:
        keys.add("rate_difference")
    if _has_breakdown_evidence(datasets=datasets, facts=facts):
        keys.update({"summary", "main_drivers"})
    elif datasets or facts:
        keys.add("summary")
    fact_paths = {str(item.get("path") or "").lower() for item in facts}
    if any("shares_remaining" in path or "remaining_shares" in path for path in fact_paths):
        keys.add("shares_remaining")
    if any("stock_cost_per_share" in path or "remaining_stock_cost_basis" in path or "cost_basis" in path for path in fact_paths):
        keys.add("cost_basis")
    if any("spot" in path or "quote_status" in path for path in fact_paths):
        keys.add("spot_freshness")
    if any("assigned_stock_unrealized_pnl" in path for path in fact_paths):
        keys.add("unrealized_pnl")
    if any("assignment_lifecycle_pnl" in path for path in fact_paths):
        keys.add("lifecycle_pnl")
    if any("current_version" in path for path in fact_paths) or _diagnostics_have_version(diagnostics, "current_version"):
        keys.add("current_version")
    if any("target_version" in path or "latest_version" in path for path in fact_paths) or _diagnostics_have_version(
        diagnostics, "target_version"
    ):
        keys.add("target_version")
    if any("command_status" in path or "operation_status" in path or "outcome_status" in path or "status" in path for path in fact_paths):
        keys.add("command_status")
    if _diagnostics_have_command_status(diagnostics):
        keys.add("command_status")
    return keys


def _key_satisfied_by_scope(key: str, task_contract: TaskContract, evidence: dict[str, Any]) -> bool:
    if key == "source_and_policy":
        return bool(evidence.get("datasets"))
    _ = task_contract
    return False


def _account_coverage_gaps(
    *,
    task_contract: TaskContract,
    evidence: dict[str, Any],
    facts: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if "account_comparison" not in task_contract.intent_families:
        return []
    requested = {str(item).strip().lower() for item in task_contract.scope.get("requested_accounts") or [] if str(item).strip()}
    if len(requested) < 2:
        return []
    covered = _covered_accounts(evidence=evidence, facts=facts, datasets=datasets)
    missing = sorted(requested - covered)
    if not missing or not covered:
        return []
    return [
        {
            "kind": "analysis_missing_account_coverage",
            "recoverable_by": "analysis_query",
            "suggested_tool": "analysis_query",
            "suggested_views": _suggested_analysis_views(datasets) or ["account_monthly_performance"],
            "missing_accounts": missing,
            "covered_accounts": sorted(covered),
            "reason": "task contract requires account comparison coverage for all named accounts",
        }
    ]


def _account_comparison_metric_gaps(
    *,
    task_contract: TaskContract,
    evidence: dict[str, Any],
    facts: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if "account_comparison" not in task_contract.intent_families:
        return []
    requested = _requested_accounts(task_contract)
    if len(requested) < 2:
        return []
    covered = _covered_accounts(evidence=evidence, facts=facts, datasets=datasets)
    if requested - covered:
        return []

    gaps: list[dict[str, Any]] = []
    if (
        ("comparison_winner" in task_contract.required_answer or "amount_difference" in task_contract.required_answer)
        and not _comparable_account_metric_groups(facts, requested_accounts=requested, rate=False)
    ):
        gaps.append(
            {
                "kind": "analysis_comparison_metric_missing",
                "recoverable_by": "analysis_query",
                "suggested_tool": "analysis_query",
                "suggested_views": _suggested_analysis_views(datasets) or ["account_monthly_performance"],
                "required_accounts": sorted(requested),
                "required_answer_keys": [
                    key
                    for key in ("comparison_winner", "amount_difference")
                    if key in task_contract.required_answer
                ],
                "reason": "task contract requires same-period, same-currency account metric evidence for comparison",
            }
        )
    if "rate_difference" in task_contract.required_answer and not _comparable_account_metric_groups(
        facts, requested_accounts=requested, rate=True
    ):
        gaps.append(
            {
                "kind": "analysis_comparison_rate_missing",
                "recoverable_by": "analysis_query",
                "suggested_tool": "analysis_query",
                "suggested_views": _suggested_analysis_views(datasets) or ["account_monthly_performance"],
                "required_accounts": sorted(requested),
                "required_answer_keys": ["rate_difference"],
                "reason": "task contract requires same-period account rate evidence for rate comparison",
            }
        )
    return gaps


def _breakdown_gaps(*, task_contract: TaskContract, datasets: list[dict[str, Any]], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if "breakdown" not in task_contract.intent_families:
        return []
    if _has_breakdown_evidence(datasets=datasets, facts=facts):
        return []
    views = _covered_views(datasets)
    if not views or not views <= {"account_monthly_performance", "monthly_income_return_summary", "monthly_income_combined_return_summary"}:
        return []
    return [
        {
            "kind": "analysis_breakdown_needed",
            "recoverable_by": "analysis_query",
            "suggested_tool": "analysis_query",
            "suggested_views": ["account_monthly_income_components", "symbol_income_attribution"],
            "reason": "task contract requires breakdown/main-driver evidence, but only summary evidence is covered",
        }
    ]


def _assigned_stock_quote_gaps(
    *,
    task_contract: TaskContract,
    missing_data: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if "assigned_stock_pnl" not in task_contract.intent_families:
        return []
    fresh_symbols = _fresh_quote_symbols(facts)
    quote_records = [
        item
        for item in missing_data
        if str(item.get("kind") or "") == "missing_quote" and str(item.get("recoverable_by") or "") == "refresh_quotes"
    ]
    if not quote_records:
        return []
    symbols: set[str] = set()
    accounts: set[str] = set()
    for record in quote_records:
        if str(record.get("symbol") or "").strip():
            symbol = str(record.get("symbol"))
            if symbol.upper() not in fresh_symbols:
                symbols.add(symbol)
        if str(record.get("account") or "").strip():
            accounts.add(str(record.get("account")))
        for symbol in record.get("symbols") or []:
            if str(symbol).strip():
                text = str(symbol)
                if text.upper() not in fresh_symbols:
                    symbols.add(text)
    if quote_records and not symbols:
        return []
    return [
        {
            "kind": "recoverable_missing_quote",
            "required_answer_key": "spot_freshness",
            "impact": "当前正股浮盈亏和生命周期 PnL 无法按实时行情计算",
            "recoverable": True,
            "recoverable_by": "refresh_quotes",
            "suggested_tool": "option_positions_read",
            "suggested_arguments": {"action": "assigned-stock", "refresh_quotes": True},
            "symbols": sorted(symbols),
            "accounts": sorted(accounts),
            "reason": "task contract requires realtime assigned-stock PnL, but quote-dependent facts are missing",
        }
    ]


def _upgrade_status_gaps(
    *,
    task_contract: TaskContract,
    provided: set[str],
    diagnostics: list[dict[str, Any]],
    missing_data: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if "upgrade_status" not in task_contract.intent_families:
        return []
    all_missing = [*missing_data, *_diagnostic_missing_data(diagnostics)]
    missing_kinds = {str(item.get("kind") or "").strip() for item in all_missing if str(item.get("kind") or "").strip()}
    observed_operation_evidence = _has_upgrade_operation_evidence(diagnostics=diagnostics, datasets=datasets)
    can_query_operation = not observed_operation_evidence and _has_upgrade_operation_identifier(task_contract=task_contract, diagnostics=diagnostics, datasets=datasets)
    gaps: list[dict[str, Any]] = []

    if "command_status" in task_contract.required_answer and "command_status" not in provided:
        gaps.append(
            _upgrade_gap(
                kind="upgrade_command_status_missing",
                required_answer_key="command_status",
                impact="无法确认升级命令当前状态",
                recoverable=can_query_operation,
            )
        )
    if ("current_version" in task_contract.required_answer and "current_version" not in provided) or "current_version_missing" in missing_kinds:
        gaps.append(
            _upgrade_gap(
                kind="upgrade_current_version_missing",
                required_answer_key="current_version",
                impact="无法展示或校验升级前当前版本",
                recoverable=can_query_operation,
            )
        )
    if ("target_version" in task_contract.required_answer and "target_version" not in provided) or "target_version_missing" in missing_kinds:
        gaps.append(
            _upgrade_gap(
                kind="upgrade_target_version_missing",
                required_answer_key="target_version",
                impact="无法展示或校验本次升级目标版本",
                recoverable=can_query_operation,
            )
        )
    if missing_kinds & {"receipt_not_observed", "final_receipt_missing"}:
        gaps.append(
            _upgrade_gap(
                kind="upgrade_receipt_missing",
                required_answer_key="receipt_status",
                impact="无法证明最终升级成功/失败回执已经送达",
                recoverable=False,
            )
        )
    if any(str(item.get("status") or "").strip() == "conflicting_evidence" for item in diagnostics):
        gaps.append(
            _upgrade_gap(
                kind="upgrade_status_conflict",
                required_answer_key="command_status",
                impact="升级命令、operation 或 release 状态存在冲突，不能给单一成功结论",
                recoverable=False,
            )
        )
    return gaps


def _upgrade_gap(*, kind: str, required_answer_key: str, impact: str, recoverable: bool) -> dict[str, Any]:
    gap: dict[str, Any] = {
        "kind": kind,
        "required_answer_key": required_answer_key,
        "impact": impact,
        "recoverable": bool(recoverable),
        "recoverable_by": "operation_timeline",
        "reason": "task contract requires upgrade status evidence, but upgrade operation evidence is incomplete",
    }
    if recoverable:
        gap["suggested_tool"] = "operation_timeline"
        gap["suggested_arguments"] = {"operation_types": ["upgrade_now"], "limit": 5}
    return gap


def _diagnostic_missing_data(diagnostics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for diagnostic in diagnostics:
        missing = diagnostic.get("missing_data")
        if isinstance(missing, list):
            out.extend(dict(item) for item in missing if isinstance(item, dict))
    return out


def _diagnostics_have_version(diagnostics: list[dict[str, Any]], field: str) -> bool:
    for diagnostic in diagnostics:
        if str(diagnostic.get("domain") or "") != "upgrade":
            continue
        version = diagnostic.get("version") if isinstance(diagnostic.get("version"), dict) else {}
        if str(version.get(field) or "").strip():
            return True
    return False


def _diagnostics_have_command_status(diagnostics: list[dict[str, Any]]) -> bool:
    for diagnostic in diagnostics:
        if str(diagnostic.get("domain") or "") != "upgrade":
            continue
        scope = diagnostic.get("scope") if isinstance(diagnostic.get("scope"), dict) else {}
        if scope.get("statuses") or str(diagnostic.get("status") or "").startswith("observed_"):
            return True
    return False


def _has_upgrade_operation_evidence(*, diagnostics: list[dict[str, Any]], datasets: list[dict[str, Any]]) -> bool:
    for diagnostic in diagnostics:
        if str(diagnostic.get("domain") or "") != "upgrade":
            continue
        source = diagnostic.get("source") if isinstance(diagnostic.get("source"), dict) else {}
        view = str(source.get("view") or diagnostic.get("view") or "").strip()
        if view in {"operation_timeline", "upgrade_operation_status"}:
            return True
    views = _covered_views(datasets)
    if "upgrade_operation_status" in views:
        return True
    return any(str(dataset.get("tool_name") or "") == "operation_timeline" for dataset in datasets)


def _has_upgrade_operation_identifier(*, task_contract: TaskContract, diagnostics: list[dict[str, Any]], datasets: list[dict[str, Any]]) -> bool:
    if _extract_upgrade_identifier(task_contract.question) or _extract_upgrade_identifier(task_contract.goal):
        return True
    for diagnostic in diagnostics:
        scope = diagnostic.get("scope") if isinstance(diagnostic.get("scope"), dict) else {}
        if scope.get("operation_ids") or scope.get("command_ids"):
            return True
    for dataset in datasets:
        payload = dataset.get("payload") if isinstance(dataset.get("payload"), dict) else {}
        if payload.get("operation_id") or payload.get("command_id"):
            return True
    return False


def _extract_upgrade_identifier(text: str) -> str:
    match = re.search(r"\b(?:in|op)_[A-Za-z0-9_:-]+\b", str(text or ""))
    return match.group(0) if match else ""


def _gap_is_recoverable(gap: dict[str, Any]) -> bool:
    if gap.get("recoverable") is False:
        return False
    return bool(str(gap.get("recoverable_by") or "").strip())


def _fresh_quote_symbols(facts: list[dict[str, Any]]) -> set[str]:
    symbols: set[str] = set()
    for fact in facts:
        path = str(fact.get("path") or "").lower()
        if "spot" not in path and "quote_status" not in path:
            continue
        if str(fact.get("freshness") or "") not in {"fresh", "not_applicable"}:
            continue
        symbol = str(fact.get("symbol") or "").strip().upper()
        if symbol:
            symbols.add(symbol)
    return symbols


def _requested_accounts(task_contract: TaskContract) -> set[str]:
    return {str(item).strip().lower() for item in task_contract.scope.get("requested_accounts") or [] if str(item).strip()}


def _comparable_account_metric_groups(
    facts: list[dict[str, Any]],
    *,
    requested_accounts: set[str],
    rate: bool,
) -> list[dict[str, Any]]:
    if len(requested_accounts) < 2:
        return []
    grouped: dict[tuple[str, str, str], dict[str, tuple[float, str]]] = {}
    for fact in facts:
        account, metric_name = _fact_account_metric(fact, requested_accounts=requested_accounts)
        if account not in requested_accounts or not metric_name:
            continue
        value = _float_value(fact.get("value"))
        if value is None:
            continue
        if not _metric_name_matches(metric_name, rate=rate):
            continue
        period = _fact_period(fact)
        currency = "RATE" if rate else _fact_currency(fact, metric_name)
        if not period or not currency:
            continue
        grouped.setdefault((_normalized_metric_name(metric_name), period, currency), {})[account] = (value, currency)
    out: list[dict[str, Any]] = []
    for (metric_name, period, currency), account_values in grouped.items():
        if requested_accounts <= set(account_values):
            out.append(
                {
                    "metric": metric_name,
                    "period": period,
                    "currency": "" if currency == "RATE" else currency,
                    "accounts": sorted(account_values),
                }
            )
    return out


def _fact_account_metric(fact: dict[str, Any], *, requested_accounts: set[str]) -> tuple[str, str]:
    field_name = _fact_field_name(fact)
    account = str(fact.get("account") or "").strip().lower()
    if account:
        return account, field_name
    for requested in sorted(requested_accounts, key=len, reverse=True):
        prefix = f"{requested}_"
        if field_name.startswith(prefix):
            return requested, field_name[len(prefix) :]
    return "", field_name


def _fact_field_name(fact: dict[str, Any]) -> str:
    source_path = str(fact.get("source_path") or "")
    if "." in source_path:
        return source_path.rsplit(".", 1)[-1].lower()
    return str(fact.get("path") or "").rsplit(".", 1)[-1].lower()


def _fact_period(fact: dict[str, Any]) -> str:
    as_of = str(fact.get("as_of") or "").strip()
    if as_of:
        return as_of
    source_path = str(fact.get("source_path") or "")
    if source_path.startswith("rows[") and "]." in source_path:
        return source_path.split("].", 1)[0] + "]"
    return ""


def _fact_currency(fact: dict[str, Any], metric_name: str) -> str:
    currency = str(fact.get("currency") or "").strip().upper()
    if currency:
        return currency
    return _currency_from_field(metric_name)


def _currency_from_field(field_name: str) -> str:
    name = str(field_name or "").lower()
    if name.endswith("_cny"):
        return "CNY"
    if name.endswith("_hkd"):
        return "HKD"
    if name.endswith("_usd"):
        return "USD"
    return ""


def _metric_name_matches(metric_name: str, *, rate: bool) -> bool:
    path = str(metric_name or "").lower()
    if rate:
        return "rate" in path or "percent" in path or "return" in path
    return any(token in path for token in ("income", "cashflow", "pnl", "premium", "amount", "gross", "market_value"))


def _normalized_metric_name(metric_name: str) -> str:
    return str(metric_name or "").lower()


def _float_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _metric_accounts(facts: list[dict[str, Any]], *, metric: str) -> set[str]:
    accounts: set[str] = set()
    for fact in facts:
        path = str(fact.get("path") or "").lower()
        value = fact.get("value")
        if not isinstance(value, (int, float)):
            continue
        account = str(fact.get("account") or "").strip().lower()
        if not account:
            continue
        if metric == "rate":
            matched = "rate" in path or "percent" in path or "return" in path
        else:
            matched = any(token in path for token in ("income", "cashflow", "pnl", "premium", "amount", "gross", "diff"))
        if matched:
            accounts.add(account)
    return accounts


def _covered_accounts(*, evidence: dict[str, Any], facts: list[dict[str, Any]], datasets: list[dict[str, Any]]) -> set[str]:
    scope = evidence.get("scope") if isinstance(evidence.get("scope"), dict) else {}
    accounts = {str(item).strip().lower() for item in scope.get("accounts") or [] if str(item).strip()}
    accounts.update(str(item.get("account") or "").strip().lower() for item in facts if str(item.get("account") or "").strip())
    for dataset in datasets:
        analysis_evidence = dataset.get("analysis_evidence") if isinstance(dataset.get("analysis_evidence"), dict) else {}
        coverage = analysis_evidence.get("coverage") if isinstance(analysis_evidence.get("coverage"), dict) else {}
        accounts.update(str(item).strip().lower() for item in coverage.get("accounts") or [] if str(item).strip())
    return accounts


def _covered_views(datasets: list[dict[str, Any]]) -> set[str]:
    views: set[str] = set()
    for dataset in datasets:
        analysis_evidence = dataset.get("analysis_evidence") if isinstance(dataset.get("analysis_evidence"), dict) else {}
        coverage = analysis_evidence.get("coverage") if isinstance(analysis_evidence.get("coverage"), dict) else {}
        views.update(str(item).strip() for item in coverage.get("views") or [] if str(item).strip())
    return views


def _suggested_analysis_views(datasets: list[dict[str, Any]]) -> list[str]:
    views = sorted(_covered_views(datasets))
    return views[:5]


def _has_breakdown_evidence(*, datasets: list[dict[str, Any]], facts: list[dict[str, Any]]) -> bool:
    views = _covered_views(datasets)
    if views & {"account_monthly_income_components", "symbol_income_attribution"}:
        return True
    paths = {str(item.get("path") or "").lower() for item in facts}
    return any("component" in path for path in paths) or (
        any("symbol" in path for path in paths) and any("amount" in path or "income" in path or "pnl" in path for path in paths)
    )


def _dedupe_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for gap in gaps:
        signature = (
            gap.get("kind"),
            tuple(gap.get("missing_accounts") or []),
            tuple(gap.get("symbols") or []),
            tuple(gap.get("suggested_views") or []),
        )
        if signature in seen:
            continue
        out.append(dict(gap))
        seen.add(signature)
    return out


__all__ = [
    "COVERAGE_RESULT_SCHEMA_VERSION",
    "CoverageResult",
    "verify_coverage",
]
