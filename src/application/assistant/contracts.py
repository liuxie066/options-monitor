from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


MESSAGE_SCHEMA_VERSION = "om-message-v1"
CONTROL_COMMAND_SCHEMA_VERSION = "om-control-command-v1"
ASSISTANT_TURN_RESULT_SCHEMA_VERSION = "om-assistant-turn-result-v1"

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
    reply_context: dict[str, Any] | None = None

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
class ControlCommand:
    """Deterministically parsed explicit command or permission response."""

    intent_name: str
    arguments: dict[str, Any]
    source: str = "deterministic"
    confidence: float = 1.0

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_COMMAND_SCHEMA_VERSION,
            "intent_name": self.intent_name,
            "arguments": dict(self.arguments),
            "source": self.source,
            "confidence": self.confidence,
        }

@dataclass(frozen=True)
class AssistantTurnResult:
    response_text: str
    render_route: str
    ok: bool
    status: str
    tool_name: str = "assistant.handle"
    error: dict[str, Any] | None = None
    permission_request: dict[str, Any] | None = None
    operation_id: str | None = None
    command_id: str | None = None
    trace: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ASSISTANT_TURN_RESULT_SCHEMA_VERSION,
            "response_text": self.response_text,
            "render_route": self.render_route,
            "ok": bool(self.ok),
            "status": self.status,
            "tool_name": self.tool_name,
            "error": dict(self.error or {}),
            "permission_request": dict(self.permission_request or {}),
            "operation_id": self.operation_id,
            "command_id": self.command_id,
            "trace": dict(self.trace or {}),
            "data": dict(self.data or {}),
            "meta": dict(self.meta or {}),
        }


__all__ = [
    "ASSISTANT_TURN_RESULT_SCHEMA_VERSION",
    "MESSAGE_SCHEMA_VERSION",
    "CONTROL_COMMAND_SCHEMA_VERSION",
    "AssistantRequest",
    "AssistantSafetyClass",
    "AssistantTurnResult",
    "ControlCommand",
]
