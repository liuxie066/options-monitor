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
        _recipe_evidence_gaps(
            task_contract=task_contract,
            facts=facts,
            datasets=datasets,
            diagnostics=diagnostics,
        )
    )
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
    if (
        any("release_status" in path or "release_published_at" in path or "github_release_url" in path for path in fact_paths)
        or _diagnostics_have_release_status(diagnostics)
    ):
        keys.add("release_status")
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
    if not _task_requires_breakdown(task_contract):
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


def _task_requires_breakdown(task_contract: TaskContract) -> bool:
    if "breakdown" in task_contract.intent_families:
        return True
    required = {str(item).strip() for item in task_contract.required_evidence if str(item).strip()}
    if required & {"driver_or_breakdown", "income_components"}:
        return True
    recipe = task_contract.selected_recipe if isinstance(task_contract.selected_recipe, dict) else {}
    recipe_needs = {str(item).strip() for item in recipe.get("evidence_needs") or [] if str(item).strip()}
    if not recipe_needs & {"driver_or_breakdown", "income_components"}:
        return False
    answer_shape = {str(item).strip() for item in task_contract.answer_shape if str(item).strip()}
    required_answer = {str(item).strip() for item in task_contract.required_answer if str(item).strip()}
    return task_contract.task_mode == "analyze" or "drivers" in answer_shape or "main_drivers" in required_answer


def _recipe_evidence_gaps(
    *,
    task_contract: TaskContract,
    facts: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    recipe = task_contract.selected_recipe if isinstance(task_contract.selected_recipe, dict) else {}
    recipe_name = str(recipe.get("name") or "").strip()
    evidence_needs = {str(item).strip() for item in recipe.get("evidence_needs") or [] if str(item).strip()}
    if not recipe_name or not evidence_needs:
        return []
    if task_contract.requested_effect == "preview_write":
        return []
    gaps: list[dict[str, Any]] = []
    if "operation_readback" in evidence_needs and not _has_operation_readback_evidence(datasets=datasets, diagnostics=diagnostics):
        gaps.append(
            _recipe_operation_gap(
                kind="recipe_operation_readback_missing",
                recipe_name=recipe_name,
                required_evidence="operation_readback",
                impact="缺少操作 readback / timeline 证据，不能证明执行状态闭环",
                task_contract=task_contract,
            )
        )
    if "receipt_status" in evidence_needs and not _has_receipt_status_evidence(facts=facts, datasets=datasets, diagnostics=diagnostics):
        gaps.append(
            _recipe_operation_gap(
                kind="recipe_receipt_status_missing",
                recipe_name=recipe_name,
                required_evidence="receipt_status",
                impact="缺少最终回执证据，不能证明结果已被观测或送达",
                task_contract=task_contract,
            )
        )
    if "audit" in evidence_needs and not _has_audit_evidence(datasets=datasets, diagnostics=diagnostics):
        gaps.append(
            _recipe_operation_gap(
                kind="recipe_audit_evidence_missing",
                recipe_name=recipe_name,
                required_evidence="audit",
                impact="缺少 trace/timeline 审计证据，不能复盘操作闭环",
                task_contract=task_contract,
            )
        )
    if "risk_premise" in evidence_needs and not _has_risk_premise_evidence(facts=facts, datasets=datasets, diagnostics=diagnostics):
        gaps.append(
            {
                "kind": "recipe_risk_premise_missing",
                "recipe_name": recipe_name,
                "required_evidence": "risk_premise",
                "recoverable_by": "analysis_query",
                "suggested_tool": "analysis_query",
                "suggested_views": ["candidate_filter_diagnostics", "close_advice_snapshot", "strategy_config_by_symbol_account"],
                "reason": "selected recipe requires risk premise evidence for strategy or candidate recommendation",
            }
        )
    if "dry_run_or_replay" in evidence_needs and not _has_strategy_replay_evidence(datasets=datasets, diagnostics=diagnostics):
        gaps.append(
            {
                "kind": "recipe_strategy_replay_evidence_missing",
                "recipe_name": recipe_name,
                "required_evidence": "dry_run_or_replay",
                "recoverable": True,
                "recoverable_by": "analysis_query",
                "suggested_tool": "analysis_query",
                "suggested_views": ["strategy_replay_read_surface"],
                "impact": "缺少 replay / dry-run 证据，策略建议只能保持前提性或说明证据不足",
                "reason": "selected recipe requires replay or dry-run evidence before giving a strategy recommendation",
            }
        )
    return gaps


def _recipe_operation_gap(
    *,
    kind: str,
    recipe_name: str,
    required_evidence: str,
    impact: str,
    task_contract: TaskContract,
) -> dict[str, Any]:
    recoverable = _has_operation_identifier(task_contract=task_contract)
    gap: dict[str, Any] = {
        "kind": kind,
        "recipe_name": recipe_name,
        "required_evidence": required_evidence,
        "impact": impact,
        "recoverable": bool(recoverable),
        "recoverable_by": "operation_timeline",
        "reason": "selected recipe requires operation lifecycle/readback evidence",
    }
    if recoverable:
        gap["suggested_tool"] = "operation_timeline"
        suggested_arguments: dict[str, Any] = {"limit": 5}
        operation_id = _operation_identifier(task_contract=task_contract)
        if operation_id:
            suggested_arguments["operation_id"] = operation_id
        gap["suggested_arguments"] = suggested_arguments
    return gap


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
    if "release_publication_status_missing" in missing_kinds:
        gaps.append(
            _upgrade_gap(
                kind="upgrade_release_publication_status_missing",
                required_answer_key="release_status",
                impact="只有 release tag，无法证明 GitHub Release 已发布或发布失败",
                recoverable=False,
                recoverable_by="release_workflow_status",
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


def _upgrade_gap(
    *,
    kind: str,
    required_answer_key: str,
    impact: str,
    recoverable: bool,
    recoverable_by: str = "operation_timeline",
) -> dict[str, Any]:
    gap: dict[str, Any] = {
        "kind": kind,
        "required_answer_key": required_answer_key,
        "impact": impact,
        "recoverable": bool(recoverable),
        "recoverable_by": recoverable_by,
        "reason": "task contract requires upgrade status evidence, but upgrade operation evidence is incomplete",
    }
    if recoverable:
        if recoverable_by == "operation_timeline":
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


def _diagnostics_have_release_status(diagnostics: list[dict[str, Any]]) -> bool:
    for diagnostic in diagnostics:
        if str(diagnostic.get("domain") or "") != "upgrade":
            continue
        if diagnostic.get("release_statuses"):
            return True
        scope = diagnostic.get("scope") if isinstance(diagnostic.get("scope"), dict) else {}
        statuses = scope.get("release_statuses")
        if statuses:
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


def _has_operation_identifier(*, task_contract: TaskContract) -> bool:
    return bool(_operation_identifier(task_contract=task_contract))


def _operation_identifier(*, task_contract: TaskContract) -> str:
    if _extract_upgrade_identifier(task_contract.question) or _extract_upgrade_identifier(task_contract.goal):
        return _extract_upgrade_identifier(task_contract.question) or _extract_upgrade_identifier(task_contract.goal)
    scope = task_contract.scope if isinstance(task_contract.scope, dict) else {}
    for key in ("operation_ids", "command_ids"):
        values = scope.get(key)
        if isinstance(values, list):
            for item in values:
                text = str(item).strip()
                if text:
                    return text
    return ""


def _has_operation_readback_evidence(*, datasets: list[dict[str, Any]], diagnostics: list[dict[str, Any]]) -> bool:
    if any(str(dataset.get("tool_name") or "") == "operation_timeline" for dataset in datasets):
        return True
    if "upgrade_operation_status" in _covered_views(datasets):
        return True
    for diagnostic in diagnostics:
        source = diagnostic.get("source") if isinstance(diagnostic.get("source"), dict) else {}
        view = str(source.get("view") or diagnostic.get("view") or "").strip()
        if view in {"operation_timeline", "upgrade_operation_status"}:
            return True
    return False


def _has_receipt_status_evidence(
    *,
    facts: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> bool:
    fact_paths = {str(item.get("path") or "").lower() for item in facts}
    if any("receipt" in path or "delivery" in path for path in fact_paths):
        return True
    if any(str(dataset.get("tool_name") or "") == "operation_timeline" for dataset in datasets):
        return True
    for diagnostic in diagnostics:
        source = diagnostic.get("source") if isinstance(diagnostic.get("source"), dict) else {}
        view = str(source.get("view") or diagnostic.get("view") or "").strip()
        if view == "operation_timeline":
            return True
        scope = diagnostic.get("scope") if isinstance(diagnostic.get("scope"), dict) else {}
        if scope.get("receipt_statuses") or diagnostic.get("receipt_statuses"):
            return True
    return False


def _has_audit_evidence(*, datasets: list[dict[str, Any]], diagnostics: list[dict[str, Any]]) -> bool:
    tools = {str(dataset.get("tool_name") or "") for dataset in datasets}
    if tools.intersection({"operation_timeline", "assistant_trace"}):
        return True
    for diagnostic in diagnostics:
        source = diagnostic.get("source") if isinstance(diagnostic.get("source"), dict) else {}
        view = str(source.get("view") or diagnostic.get("view") or "").strip()
        if view in {"operation_timeline", "assistant_trace"}:
            return True
    return False


def _has_risk_premise_evidence(
    *,
    facts: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> bool:
    views = _covered_views(datasets)
    if views.intersection({"candidate_filter_diagnostics", "close_advice_snapshot", "strategy_config_by_symbol_account"}):
        return True
    fact_paths = {str(item.get("path") or "").lower() for item in facts}
    if any(token in path for path in fact_paths for token in ("risk", "delta", "iv", "rv", "drawdown", "volatility", "score")):
        return True
    return any(str(item.get("domain") or "") in {"candidate", "close_advice", "runtime"} for item in diagnostics)


def _has_strategy_replay_evidence(
    *,
    datasets: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> bool:
    if _has_observed_strategy_replay_diagnostic(diagnostics):
        return True
    if "strategy_replay_read_surface" in _covered_views(datasets):
        has_rows = any(
            "strategy_replay_read_surface" in _dataset_views(dataset)
            and _dataset_row_count(dataset) > 0
            and _dataset_has_strategy_replay_evidence_shape(dataset)
            for dataset in datasets
        )
        if has_rows and not _has_missing_strategy_replay_diagnostic(diagnostics):
            return True
    return False


def _has_observed_strategy_replay_diagnostic(diagnostics: list[dict[str, Any]]) -> bool:
    for diagnostic in diagnostics:
        source = diagnostic.get("source") if isinstance(diagnostic.get("source"), dict) else {}
        view = str(source.get("view") or diagnostic.get("view") or "").strip()
        domain = str(diagnostic.get("domain") or "").strip()
        status = str(diagnostic.get("status") or "").strip()
        if (view == "strategy_replay_read_surface" or domain == "strategy_replay") and status == "observed_strategy_replay_evidence":
            return True
    return False


def _has_missing_strategy_replay_diagnostic(diagnostics: list[dict[str, Any]]) -> bool:
    missing_statuses = {"diagnostic_missing", "artifact_missing", "empty_artifact", "read_error", "no_matching_rows"}
    for diagnostic in diagnostics:
        source = diagnostic.get("source") if isinstance(diagnostic.get("source"), dict) else {}
        view = str(source.get("view") or diagnostic.get("view") or "").strip()
        domain = str(diagnostic.get("domain") or "").strip()
        status = str(diagnostic.get("status") or "").strip()
        if (view == "strategy_replay_read_surface" or domain == "strategy_replay") and status in missing_statuses:
            return True
    return False


def _gap_is_recoverable(gap: dict[str, Any]) -> bool:
    if gap.get("recoverable") is False:
        return False
    recoverable_by = str(gap.get("recoverable_by") or "").strip()
    suggested_tool = str(gap.get("suggested_tool") or "").strip()
    return bool(recoverable_by and suggested_tool)


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
        views.update(_dataset_views(dataset))
    return views


def _dataset_views(dataset: dict[str, Any]) -> set[str]:
    analysis_evidence = dataset.get("analysis_evidence") if isinstance(dataset.get("analysis_evidence"), dict) else {}
    coverage = analysis_evidence.get("coverage") if isinstance(analysis_evidence.get("coverage"), dict) else {}
    return {str(item).strip() for item in coverage.get("views") or [] if str(item).strip()}


def _dataset_row_count(dataset: dict[str, Any]) -> int:
    try:
        return int(dataset.get("row_count") or 0)
    except Exception:
        return 0


def _dataset_columns(dataset: dict[str, Any]) -> set[str]:
    return {str(item).strip() for item in dataset.get("columns") or [] if str(item).strip()}


def _dataset_has_strategy_replay_evidence_shape(dataset: dict[str, Any]) -> bool:
    evidence_columns = {
        "artifact_kind",
        "status",
        "data_mode",
        "dataset_id",
        "selected_run_ids",
        "candidate_snapshot_count",
        "filter_decision_count",
        "decision_instance_count",
        "underwriting_candidate_count",
        "mark_path_snapshot_count",
        "usable_mark_path_snapshot_count",
        "outcome_fact_count",
        "evidence_level",
        "strict_backtest_allowed",
        "candidate_impact_allowed",
        "production_recommendation_allowed",
        "dry_run_patch_allowed",
        "recommended_variant",
        "best_variant",
        "strategy_family",
        "confidence",
        "next_action",
        "limitations",
        "safety_summary",
    }
    return bool(_dataset_columns(dataset) & evidence_columns)


def _suggested_analysis_views(datasets: list[dict[str, Any]]) -> list[str]:
    views = sorted(_covered_views(datasets))
    return views[:5]


def _has_breakdown_evidence(*, datasets: list[dict[str, Any]], facts: list[dict[str, Any]]) -> bool:
    views = _covered_views(datasets)
    if views & {"account_monthly_income_components", "symbol_income_attribution"}:
        return True
    paths = {str(item.get("path") or "").lower() for item in facts}
    if any(
        path.startswith(("cashflow_rows[]", "realized_rows[]", "premium_rows[]"))
        for path in paths
    ) and any("symbol" in path for path in paths):
        return True
    return any("component" in path for path in paths) or (
        any("symbol" in path for path in paths)
        and any(
            "amount" in path
            or "income" in path
            or "pnl" in path
            or "cashflow" in path
            or "premium" in path
            or "realized" in path
            for path in paths
        )
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
