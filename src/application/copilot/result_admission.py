from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from src.application.copilot.contracts import AnswerReport, AppResult
from src.application.copilot.safety_text import contains_forbidden_external_action_claim


VALID_STATUSES = {
    "answered",
    "needs_clarification",
    "insufficient_evidence",
    "cancelled",
    "refused",
    "not_ready",
    "failed",
}

@dataclass(frozen=True)
class AdmissionDecision:
    result: AppResult
    rejection_reason: str | None = None


def admit_result(result: AppResult) -> AppResult:
    return admit_result_with_decision(result).result


def admit_result_with_decision(result: AppResult) -> AdmissionDecision:
    response = str(result.user_response or "").strip()
    conclusion = str(result.answer_report.conclusion or "").strip() if result.answer_report else ""
    check_text = response or conclusion
    if result.status not in VALID_STATUSES:
        return _reject(result, "invalid_status")
    if result.status != "failed" and not check_text:
        return _reject(result, "empty_result")
    if result.status == "answered" and not check_text.startswith("结论"):
        return _reject(result, "missing_conclusion_prefix")
    if result.answer_report and not _valid_report_lists(result.answer_report):
        return _reject(result, "malformed_report_fields")
    if result.answer_report and not _valid_items_with_refs(result.answer_report.findings, "evidence_refs"):
        return _reject(result, "malformed_findings")
    if result.answer_report and not _valid_recommendations(result.answer_report.recommendations):
        return _reject(result, "malformed_recommendations")
    if contains_forbidden_external_action_claim(_claim_text(result)):
        return _reject(
            result,
            "mutation_claim",
            "结论：Copilot 结果未通过安全校验，因为它声称执行了写入或外部操作。",
        )
    if result.status == "answered" and result.answer_report and not result.answer_report.attempted_checks:
        return _reject(result, "missing_attempted_checks")
    return AdmissionDecision(result=result)


def _claim_text(result: AppResult) -> str:
    parts = [str(result.user_response or "")]
    if result.answer_report:
        parts.append(str(result.answer_report.conclusion or ""))
        parts.extend(str(item or "") for item in result.answer_report.attempted_checks)
        parts.extend(str(item or "") for item in result.answer_report.missing_data)
        parts.extend(str(item or "") for item in result.answer_report.evidence_refs)
        parts.extend(str(item.get("summary") or "") for item in result.answer_report.findings)
        parts.extend(
            str(ref or "")
            for item in result.answer_report.findings
            for ref in item.get("evidence_refs", [])
            if isinstance(item.get("evidence_refs"), list)
        )
        for item in result.answer_report.recommendations:
            parts.append(str(item.get("summary") or ""))
            parts.append(str(item.get("action") or ""))
            parts.append(str(item.get("target_scope") or ""))
            parts.append(str(item.get("answer_dimension") or ""))
            if isinstance(item.get("basis_refs"), list):
                parts.extend(str(ref or "") for ref in item.get("basis_refs", []))
    return "\n".join(parts)


def _valid_items_with_refs(items: Any, ref_field: str) -> bool:
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        summary = item.get("summary", "")
        if "summary" in item and not isinstance(summary, str):
            return False
        refs = item.get(ref_field)
        if not _string_list(refs) or not refs:
            return False
        if not str(summary or "").strip():
            return False
    return True


def _valid_recommendations(items: Any) -> bool:
    if not _valid_items_with_refs(items, "basis_refs"):
        return False
    for item in items:
        if not (
            isinstance(item.get("action"), str)
            and bool(item.get("action", "").strip())
            and isinstance(item.get("target_scope"), str)
            and bool(item.get("target_scope", "").strip())
        ):
            return False
        if item.get("answer_dimension") is not None and not isinstance(item.get("answer_dimension"), str):
            return False
    return True


def _valid_report_lists(report: AnswerReport) -> bool:
    return (
        _string_list(report.attempted_checks)
        and _string_list(report.missing_data)
        and _string_list(report.evidence_refs)
    )


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _reject(result: AppResult, reason: str, user_response: str = "") -> AdmissionDecision:
    return AdmissionDecision(result=_failed_result(result, reason, user_response), rejection_reason=reason)


def _failed_result(result: AppResult, reason: str, user_response: str) -> AppResult:
    return AppResult(
        status="failed",
        answer_report=AnswerReport(
            conclusion=user_response or "结论：Copilot 结果未通过结构或安全校验。",
            missing_data=[reason],
        ),
        request_id=result.request_id,
        contract_id=result.contract_id,
        run_id=result.run_id,
        events=result.events,
        decision_trace=result.decision_trace,
        ok=False,
    )
