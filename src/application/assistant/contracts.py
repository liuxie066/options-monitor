from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ASSISTANT_FRAME_SCHEMA_VERSION = "om-assistant-frame-v1"
SEMANTIC_FRAME_SCHEMA_VERSION = "om-semantic-frame-v1"
TOOL_PLAN_SCHEMA_VERSION = "om-tool-plan-v1"
AssistantSafetyClass = Literal["read", "write_preview", "write_apply", "admin_preview", "local"]


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
            "channel": self.channel,
            "sender_id": self.sender_id,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "config_key": self.config_key,
            "config_path": self.config_path,
        }


@dataclass(frozen=True)
class SemanticFrame:
    """Canonical semantic output from command, deterministic parser, or LLM.

    A semantic frame describes what the user wants in assistant vocabulary. It
    intentionally carries no tool name and no executable payload. Tool planning
    belongs to src.application.assistant.frame_planner.
    """

    name: str
    arguments: dict[str, Any]
    parser: str = "deterministic"
    confidence: float = 1.0

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SEMANTIC_FRAME_SCHEMA_VERSION,
            "name": self.name,
            "arguments": dict(self.arguments),
            "parser": self.parser,
            "confidence": self.confidence,
        }


AssistantIntent = SemanticFrame


@dataclass(frozen=True)
class AssistantFrame:
    intent: str
    payload: dict[str, Any]
    safety_class: AssistantSafetyClass
    parser: str = "deterministic"
    confidence: float = 1.0

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ASSISTANT_FRAME_SCHEMA_VERSION,
            "intent": self.intent,
            "payload": dict(self.payload),
            "safety_class": self.safety_class,
            "parser": self.parser,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class AssistantToolCall:
    tool_name: str
    payload: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class ToolPlan:
    tool_name: str
    payload: dict[str, Any]
    safety_class: AssistantSafetyClass
    read_only: bool = True
    requires_confirmation: bool = False
    reason: str = ""
    source_intent: str | None = None

    def to_tool_call(self) -> AssistantToolCall:
        return AssistantToolCall(tool_name=self.tool_name, payload=dict(self.payload))

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TOOL_PLAN_SCHEMA_VERSION,
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
            "safety_class": self.safety_class,
            "read_only": bool(self.read_only),
            "requires_confirmation": bool(self.requires_confirmation),
            "reason": self.reason,
            "source_intent": self.source_intent,
        }


__all__ = [
    "ASSISTANT_FRAME_SCHEMA_VERSION",
    "SEMANTIC_FRAME_SCHEMA_VERSION",
    "TOOL_PLAN_SCHEMA_VERSION",
    "AssistantFrame",
    "AssistantIntent",
    "AssistantRequest",
    "AssistantSafetyClass",
    "AssistantToolCall",
    "SemanticFrame",
    "ToolPlan",
]
