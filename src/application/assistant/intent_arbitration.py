from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_error_payload
from src.application.assistant.contracts import AssistantIntent


INTENT_ARBITRATION_SCHEMA_VERSION = "om-intent-arbitration-v1"
ASSISTANT_DECISION_SCHEMA_VERSION = "om-assistant-decision-v1"


@dataclass(frozen=True)
class IntentCandidate:
    source: str
    status: str
    intent: AssistantIntent | None = None
    reason: str | None = None
    error: AgentToolError | None = None

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "status": self.status,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.intent is not None:
            payload["intent"] = self.intent.public_payload()
            payload["intent_name"] = self.intent.name
            payload["parser"] = self.intent.parser
            payload["confidence"] = self.intent.confidence
        if self.error is not None:
            payload["error"] = build_error_payload(self.error)
            payload["error_code"] = self.error.code
        return payload


@dataclass(frozen=True)
class IntentArbitration:
    decision: str
    selected_source: str | None
    selected_intent: AssistantIntent | None
    candidates: tuple[IntentCandidate, ...]

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": INTENT_ARBITRATION_SCHEMA_VERSION,
            "decision": self.decision,
            "selected_source": self.selected_source,
            "selected_intent": self.selected_intent.public_payload() if self.selected_intent else None,
            "conflict": _has_conflicting_accepted_candidates(self.candidates),
            "candidates": [candidate.public_payload() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class AssistantDecision:
    route: str
    arbitration: IntentArbitration | None
    llm_trace: dict[str, Any]
    intent_metadata: dict[str, Any] | None = None

    def public_payload(self) -> dict[str, Any]:
        selected_intent = self.arbitration.selected_intent if self.arbitration else None
        metadata = dict(self.intent_metadata or {})
        llm_trace = dict(self.llm_trace or {})
        return {
            "schema_version": ASSISTANT_DECISION_SCHEMA_VERSION,
            "route": self.route,
            "selected_source": self.arbitration.selected_source if self.arbitration else None,
            "selected_intent_name": selected_intent.name if selected_intent else None,
            "selected_parser": selected_intent.parser if selected_intent else None,
            "selected_confidence": selected_intent.confidence if selected_intent else None,
            "arbitration_decision": self.arbitration.decision if self.arbitration else None,
            "candidate_count": len(self.arbitration.candidates) if self.arbitration else 0,
            "llm": {
                "attempted": bool(llm_trace.get("attempted", False)),
                "reason": str(llm_trace.get("reason") or ""),
                "provider": str(llm_trace.get("provider") or ""),
                "model": str(llm_trace.get("model") or ""),
            },
            "execution_contract": _execution_contract(metadata),
        }


def accepted_candidate(source: str, intent: AssistantIntent) -> IntentCandidate:
    return IntentCandidate(source=source, status="accepted", intent=intent)


def error_candidate(source: str, err: AgentToolError, *, reason: str | None = None) -> IntentCandidate:
    return IntentCandidate(source=source, status="rejected", reason=reason, error=err)


def skipped_candidate(source: str, reason: str) -> IntentCandidate:
    return IntentCandidate(source=source, status="skipped", reason=reason)


def build_intent_arbitration(
    *,
    decision: str,
    selected_source: str | None,
    selected_intent: AssistantIntent | None,
    candidates: list[IntentCandidate] | tuple[IntentCandidate, ...],
) -> IntentArbitration:
    return IntentArbitration(
        decision=decision,
        selected_source=selected_source,
        selected_intent=selected_intent,
        candidates=tuple(candidates),
    )


def build_assistant_decision(
    *,
    route: str,
    arbitration: IntentArbitration | None,
    llm_trace: dict[str, Any],
    intent_metadata: dict[str, Any] | None = None,
) -> AssistantDecision:
    return AssistantDecision(
        route=route,
        arbitration=arbitration,
        llm_trace=dict(llm_trace or {}),
        intent_metadata=dict(intent_metadata or {}),
    )


def _has_conflicting_accepted_candidates(candidates: tuple[IntentCandidate, ...]) -> bool:
    accepted = [candidate.intent.public_payload() for candidate in candidates if candidate.status == "accepted" and candidate.intent]
    if len(accepted) < 2:
        return False
    first = accepted[0]
    return any(item != first for item in accepted[1:])


def _execution_contract(metadata: dict[str, Any]) -> dict[str, Any]:
    read_only = metadata.get("read_only")
    risk_level = str(metadata.get("risk_level") or ("read_only" if read_only is True else "unknown"))
    return {
        "read_only": read_only if isinstance(read_only, bool) else None,
        "risk_level": risk_level,
        "operation_action": metadata.get("operation_action"),
        "operation_target": metadata.get("operation_target"),
        "llm_allowed": bool(metadata.get("llm_allowed", False)),
        "direct_writes_allowed": False,
        "llm_write_allowed": False,
        "preview_confirm_required": risk_level in {"preview_write", "preview_admin", "confirm_write"},
        "canonical_renderer_required": True,
    }


__all__ = [
    "INTENT_ARBITRATION_SCHEMA_VERSION",
    "ASSISTANT_DECISION_SCHEMA_VERSION",
    "AssistantDecision",
    "IntentArbitration",
    "IntentCandidate",
    "accepted_candidate",
    "build_assistant_decision",
    "build_intent_arbitration",
    "error_candidate",
    "skipped_candidate",
]
