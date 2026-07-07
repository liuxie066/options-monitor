from __future__ import annotations

from typing import Any

from src.application.assistant_copilot.evidence_ledger import EvidenceLedger
from src.application.assistant_copilot.task_frame import TaskFrame


VERIFICATION_SCHEMA_VERSION = "om-copilot-answer-verification-v1"
_EXECUTED_WRITE_PHRASES = (
    "已执行",
    "已修改",
    "已写入",
    "已发送",
    "已下单",
    "already executed",
    "already changed",
    "already sent",
)


def verify_answer(answer: dict[str, Any], *, frame: TaskFrame, ledger: EvidenceLedger) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    refs = ledger.known_refs()
    status = str(answer.get("status") or "")
    response_text = str(answer.get("response_text") or "")

    if status == "answered" and not str(answer.get("conclusion") or "").strip():
        failures.append({"code": "missing_conclusion", "message": "answered response requires a conclusion"})
    if status == "answered" and not response_text.strip():
        failures.append({"code": "missing_response_text", "message": "answered response requires response_text"})
    if frame.answer_shape.get("requires_evidence") and status == "answered":
        for idx, finding in enumerate(answer.get("findings") or []):
            _check_refs(finding.get("evidence_refs"), refs, failures, path=f"findings[{idx}].evidence_refs")
    if frame.answer_shape.get("requires_recommendations") and status == "answered" and not answer.get("recommendations"):
        failures.append({"code": "missing_recommendations", "message": "answer requires recommendations"})
    for idx, recommendation in enumerate(answer.get("recommendations") or []):
        if recommendation.get("policy_judgement") is True:
            continue
        _check_refs(recommendation.get("basis_refs"), refs, failures, path=f"recommendations[{idx}].basis_refs")
    lowered = response_text.lower()
    if any(phrase in response_text or phrase in lowered for phrase in _EXECUTED_WRITE_PHRASES):
        failures.append({
            "code": "write_execution_claim",
            "message": "Copilot answer must not claim write/config/notification/broker actions were executed",
        })

    return {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "ok": not failures,
        "failures": failures,
    }


def _check_refs(raw_refs: Any, known_refs: set[str], failures: list[dict[str, Any]], *, path: str) -> None:
    refs = [str(item).strip() for item in raw_refs or [] if str(item).strip()] if isinstance(raw_refs, list) else []
    if not refs:
        failures.append({"code": "missing_evidence_ref", "message": f"{path} requires at least one evidence ref"})
        return
    missing = [ref for ref in refs if ref not in known_refs]
    if missing:
        failures.append({"code": "unknown_evidence_ref", "message": f"{path} contains unknown evidence refs", "refs": missing})


__all__ = ["VERIFICATION_SCHEMA_VERSION", "verify_answer"]
