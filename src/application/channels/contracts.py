from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChannelCapabilities:
    outbound: bool = False
    inbound: bool = False
    reply: bool = False
    bind: bool = False
    health: bool = False


@dataclass(frozen=True)
class IncomingChannelMessage:
    channel: str
    sender_id: str
    text: str
    message_id: str | None = None
    conversation_id: str | None = None
    context: dict[str, Any] | None = None


@dataclass(frozen=True)
class ChannelBinding:
    channel: str
    label: str
    name: str
    target: str
    context_token: str
    to_user_id: str
    group_id: str | None = None
    chat_key: str | None = None
    updated_at_utc: str | None = None


@dataclass(frozen=True)
class DeliveryReceipt:
    channel: str
    ok: bool
    command_ok: bool
    delivery_confirmed: bool
    message_id: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None
