from __future__ import annotations

from typing import Any


ANSWER_SCHEMA_VERSION = "om-copilot-answer-v1"


def normalize_answer_payload(payload: dict[str, Any]) -> dict[str, Any]:
    answer = dict(payload or {})
    answer["schema_version"] = ANSWER_SCHEMA_VERSION
    status = str(answer.get("status") or "failed").strip()
    if status not in {"answered", "needs_clarification", "insufficient_evidence", "failed"}:
        status = "failed"
    answer["status"] = status
    answer["conclusion"] = str(answer.get("conclusion") or "").strip()
    answer["response_text"] = str(answer.get("response_text") or "").strip()
    answer["findings"] = _list(answer.get("findings"))
    answer["recommendations"] = _list(answer.get("recommendations"))
    answer["missing_data"] = _list(answer.get("missing_data"))
    return answer


def answer_instructions() -> str:
    return (
        "You compose the final Chinese answer for OM Copilot from the supplied evidence ledger only. "
        "Start with a direct conclusion. Findings and recommendations must cite exact evidence refs. "
        "If evidence is incomplete, say what is missing. Tables are optional supporting evidence, not the main answer. "
        "Do not claim that any write/config/notification/broker action was executed."
    )


def answer_json_schema() -> dict[str, Any]:
    evidence_ref_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "status", "conclusion", "findings", "recommendations", "missing_data", "response_text"],
        "properties": {
            "schema_version": {"type": "string", "const": ANSWER_SCHEMA_VERSION},
            "status": {"type": "string", "enum": ["answered", "needs_clarification", "insufficient_evidence", "failed"]},
            "conclusion": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": ["claim", "evidence_refs"],
                    "properties": {
                        "claim": {"type": "string"},
                        "evidence_refs": evidence_ref_array,
                    },
                },
            },
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": ["text", "basis_refs"],
                    "properties": {
                        "text": {"type": "string"},
                        "basis_refs": evidence_ref_array,
                        "policy_judgement": {"type": "boolean"},
                    },
                },
            },
            "missing_data": {"type": "array", "items": {"type": "string"}},
            "response_text": {"type": "string"},
        },
    }


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


__all__ = [
    "ANSWER_SCHEMA_VERSION",
    "answer_instructions",
    "answer_json_schema",
    "normalize_answer_payload",
]
