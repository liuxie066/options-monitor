from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


MESSAGE_SCHEMA_VERSION = "om-message-v1"
PERCEPTION_RESULT_SCHEMA_VERSION = "om-perception-result-v1"
REASONING_RESOLUTION_SCHEMA_VERSION = "om-reasoning-resolution-v1"
ACTION_RESULT_SCHEMA_VERSION = "om-action-result-v1"
OBSERVATION_RESPONSE_SCHEMA_VERSION = "om-observation-response-v1"

AssistantSafetyClass = Literal["read", "write_preview", "write_apply", "admin_preview", "local"]
ReasoningStatus = Literal["supported", "preview_required", "unsupported", "clarify", "denied", "failed"]
ActionKind = Literal["tool", "operation", "pending", "local_response", "none"]


@dataclass(frozen=True)
class AssistantRequest:
    text: str
    sender_id: str
    channel: str = "local"
    message_id: str | None = None
    conversation_id: str | None = None
    config_key: str | None = None
    config_path: str | None = None
    audit_db: str | None = None
    assistant_config_path: str | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": MESSAGE_SCHEMA_VERSION,
            "channel": self.channel,
            "sender_id": self.sender_id,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "config_key": self.config_key,
            "config_path": self.config_path,
        }


@dataclass(frozen=True)
class PerceptionResult:
    """Canonical perception output from command, deterministic command aliases, or LLM.

    Perception only describes what the user appears to want. It must not choose
    tools, downgrade unsupported requests to nearby capabilities, or apply write
    policy. Those decisions belong to the reasoning layer.
    """

    intent_name: str
    arguments: dict[str, Any]
    source: str = "deterministic"
    confidence: float = 1.0
    evidence: dict[str, Any] | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PERCEPTION_RESULT_SCHEMA_VERSION,
            "intent_name": self.intent_name,
            "arguments": dict(self.arguments),
            "source": self.source,
            "confidence": self.confidence,
            "evidence": dict(self.evidence or {}),
        }


@dataclass(frozen=True)
class ToolCall:
    tool_name: str
    payload: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class ReasoningResolution:
    status: ReasoningStatus
    intent_name: str | None
    arguments: dict[str, Any]
    safety_class: AssistantSafetyClass
    action_kind: ActionKind = "none"
    tool_call: ToolCall | None = None
    read_only: bool = True
    requires_confirmation: bool = False
    reason: str = ""
    message: str | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": REASONING_RESOLUTION_SCHEMA_VERSION,
            "status": self.status,
            "intent_name": self.intent_name,
            "arguments": dict(self.arguments),
            "safety_class": self.safety_class,
            "action_kind": self.action_kind,
            "tool_call": self.tool_call.public_payload() if self.tool_call else None,
            "read_only": bool(self.read_only),
            "requires_confirmation": bool(self.requires_confirmation),
            "reason": self.reason,
            "message": self.message,
        }


@dataclass(frozen=True)
class ActionResult:
    executed: bool
    ok: bool
    action_kind: ActionKind
    tool_name: str = ""
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    response_text: str | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ACTION_RESULT_SCHEMA_VERSION,
            "executed": bool(self.executed),
            "ok": bool(self.ok),
            "action_kind": self.action_kind,
            "tool_name": self.tool_name,
            "payload": dict(self.payload or {}),
            "result": dict(self.result or {}),
            "error": dict(self.error or {}),
            "response_text": self.response_text,
        }


@dataclass(frozen=True)
class ObservationResponse:
    response_text: str
    ok: bool
    status: str
    error_code: str | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": OBSERVATION_RESPONSE_SCHEMA_VERSION,
            "response_text": self.response_text,
            "ok": bool(self.ok),
            "status": self.status,
            "error_code": self.error_code,
        }


__all__ = [
    "ACTION_RESULT_SCHEMA_VERSION",
    "MESSAGE_SCHEMA_VERSION",
    "OBSERVATION_RESPONSE_SCHEMA_VERSION",
    "PERCEPTION_RESULT_SCHEMA_VERSION",
    "REASONING_RESOLUTION_SCHEMA_VERSION",
    "ActionKind",
    "ActionResult",
    "AssistantRequest",
    "AssistantSafetyClass",
    "ObservationResponse",
    "PerceptionResult",
    "ReasoningResolution",
    "ReasoningStatus",
    "ToolCall",
]
