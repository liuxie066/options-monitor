from __future__ import annotations
from dataclasses import dataclass
from src.application.copilot.contracts import AppResult


VALID_STATUSES = {
    "answered",
    "needs_clarification",
    "insufficient_evidence",
    "cancelled",
    "refused",
    "not_ready",
    "failed",
    "control_requested",
}

TOOL_PROTOCOL_MARKERS = (
    "<tool_calls>",
    "<｜｜DSML｜｜tool_calls>",
    "<｜｜DSML｜｜invoke",
)

@dataclass(frozen=True)
class AdmissionDecision:
    result: AppResult
    rejection_reason: str | None = None


def admit_result(result: AppResult) -> AppResult:
    return admit_result_with_decision(result).result


def admit_result_with_decision(result: AppResult) -> AdmissionDecision:
    response = str(result.user_response or "").strip()
    if result.status not in VALID_STATUSES:
        return _reject(result, "invalid_status")
    if result.status not in {"failed", "control_requested"} and not response:
        return _reject(result, "empty_result")
    if result.status == "answered" and any(marker in response for marker in TOOL_PROTOCOL_MARKERS):
        return _reject(result, "unparsed_tool_protocol")
    return AdmissionDecision(result=result)


def _reject(result: AppResult, reason: str, user_response: str = "") -> AdmissionDecision:
    return AdmissionDecision(result=_failed_result(result, reason, user_response), rejection_reason=reason)


def _failed_result(result: AppResult, reason: str, user_response: str) -> AppResult:
    return AppResult(
        status="failed",
        user_response=user_response or "Copilot 结果未通过结构或安全校验。",
        error={"code": "RESULT_REJECTED", "reason": reason},
        request_id=result.request_id,
        contract_id=result.contract_id,
        run_id=result.run_id,
        events=result.events,
        decision_trace=result.decision_trace,
        ok=False,
    )
