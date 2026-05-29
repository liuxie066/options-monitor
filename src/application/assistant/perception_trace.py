from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_error_payload
from src.application.assistant.contracts import PerceptionResult


PERCEPTION_TRACE_SCHEMA_VERSION = "om-perception-trace-v1"
ASSISTANT_DECISION_SCHEMA_VERSION = "om-assistant-decision-v2"


@dataclass(frozen=True)
class PerceptionCandidate:
    source: str
    status: str
    perception: PerceptionResult | None = None
    reason: str | None = None
    error: AgentToolError | None = None

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "status": self.status,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.perception is not None:
            payload["perception"] = self.perception.public_payload()
            payload["intent_name"] = self.perception.intent_name
            payload["perception_source"] = self.perception.source
            payload["confidence"] = self.perception.confidence
        if self.error is not None:
            payload["error"] = build_error_payload(self.error)
            payload["error_code"] = self.error.code
        return payload


@dataclass(frozen=True)
class PerceptionTrace:
    decision: str
    selected_source: str | None
    selected_perception: PerceptionResult | None
    candidates: tuple[PerceptionCandidate, ...]

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PERCEPTION_TRACE_SCHEMA_VERSION,
            "decision": self.decision,
            "selected_source": self.selected_source,
            "selected_perception": self.selected_perception.public_payload() if self.selected_perception else None,
            "conflict": _has_conflicting_accepted_candidates(self.candidates),
            "candidates": [candidate.public_payload() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class AssistantDecision:
    route: str
    perception_trace: PerceptionTrace | None
    llm_trace: dict[str, Any]
    intent_metadata: dict[str, Any] | None = None

    def public_payload(self) -> dict[str, Any]:
        selected = self.perception_trace.selected_perception if self.perception_trace else None
        metadata = dict(self.intent_metadata or {})
        llm_trace = dict(self.llm_trace or {})
        return {
            "schema_version": ASSISTANT_DECISION_SCHEMA_VERSION,
            "route": self.route,
            "selected_source": self.perception_trace.selected_source if self.perception_trace else None,
            "selected_intent_name": selected.intent_name if selected else None,
            "selected_perception_source": selected.source if selected else None,
            "selected_confidence": selected.confidence if selected else None,
            "perception_decision": self.perception_trace.decision if self.perception_trace else None,
            "candidate_count": len(self.perception_trace.candidates) if self.perception_trace else 0,
            "llm": {
                "attempted": bool(llm_trace.get("attempted", False)),
                "reason": str(llm_trace.get("reason") or ""),
                "provider": str(llm_trace.get("provider") or ""),
                "model": str(llm_trace.get("model") or ""),
            },
            "execution_contract": _execution_contract(metadata),
        }


def accepted_candidate(source: str, perception: PerceptionResult) -> PerceptionCandidate:
    return PerceptionCandidate(source=source, status="accepted", perception=perception)


def error_candidate(source: str, err: AgentToolError, *, reason: str | None = None) -> PerceptionCandidate:
    return PerceptionCandidate(source=source, status="rejected", reason=reason, error=err)


def skipped_candidate(source: str, reason: str) -> PerceptionCandidate:
    return PerceptionCandidate(source=source, status="skipped", reason=reason)


def build_perception_trace(
    *,
    decision: str,
    selected_source: str | None,
    selected_perception: PerceptionResult | None,
    candidates: list[PerceptionCandidate] | tuple[PerceptionCandidate, ...],
) -> PerceptionTrace:
    return PerceptionTrace(
        decision=decision,
        selected_source=selected_source,
        selected_perception=selected_perception,
        candidates=tuple(candidates),
    )


def build_assistant_decision(
    *,
    route: str,
    perception_trace: PerceptionTrace | None,
    llm_trace: dict[str, Any],
    intent_metadata: dict[str, Any] | None = None,
) -> AssistantDecision:
    return AssistantDecision(
        route=route,
        perception_trace=perception_trace,
        llm_trace=dict(llm_trace or {}),
        intent_metadata=dict(intent_metadata or {}),
    )


def _has_conflicting_accepted_candidates(candidates: tuple[PerceptionCandidate, ...]) -> bool:
    accepted = [
        candidate.perception.public_payload()
        for candidate in candidates
        if candidate.status == "accepted" and candidate.perception
    ]
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
        "supported": bool(metadata.get("supported", False)),
        "direct_writes_allowed": False,
        "llm_write_allowed": False,
        "preview_confirm_required": risk_level in {"preview_write", "preview_admin", "confirm_write"},
        "canonical_renderer_required": True,
    }


__all__ = [
    "ASSISTANT_DECISION_SCHEMA_VERSION",
    "PERCEPTION_TRACE_SCHEMA_VERSION",
    "AssistantDecision",
    "PerceptionCandidate",
    "PerceptionTrace",
    "accepted_candidate",
    "build_assistant_decision",
    "build_perception_trace",
    "error_candidate",
    "skipped_candidate",
]
