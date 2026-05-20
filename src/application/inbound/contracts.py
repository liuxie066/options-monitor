from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundRequest:
    text: str
    sender_id: str
    channel: str = "local"
    message_id: str | None = None
    conversation_id: str | None = None
    config_key: str | None = "us"
    config_path: str | None = None
    audit_db: str | None = None

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
class InboundIntent:
    name: str
    arguments: dict[str, Any]
    parser: str = "deterministic"
    confidence: float = 1.0

    def public_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": dict(self.arguments),
            "parser": self.parser,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class InboundToolCall:
    tool_name: str
    payload: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
        }
