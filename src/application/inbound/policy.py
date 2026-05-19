from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_registry import get_tool_definition
from src.application.inbound.contracts import InboundToolCall


PURE_READ_TOOLS = frozenset(
    {
        "runtime_status",
        "healthcheck",
        "option_positions_read",
        "monthly_income_report",
        "runtime_runs",
        "runtime_logs",
        "config_validate",
    }
)


@dataclass(frozen=True)
class SenderDecision:
    allowed: bool
    reason: str
    matched_entry: str | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "matched_entry": self.matched_entry,
        }


def check_sender_allowed(
    *,
    channel: str,
    sender_id: str,
    allowed_senders: str | None = None,
    require_local_allowlist: bool | None = None,
) -> SenderDecision:
    normalized_channel = str(channel or "").strip().lower() or "local"
    normalized_sender = str(sender_id or "").strip()
    raw = allowed_senders if allowed_senders is not None else _default_allowed_senders(normalized_channel)
    entries = _parse_allowed_entries(raw)

    if normalized_channel == "local" and not _truthy_env("OM_INBOUND_REQUIRE_ALLOWLIST", require_local_allowlist):
        return SenderDecision(allowed=True, reason="local_channel_allowed")

    if not normalized_sender:
        return SenderDecision(allowed=False, reason="missing_sender")

    for entry_channel, entry_sender, original in entries:
        channel_ok = entry_channel == "*" or entry_channel == normalized_channel
        sender_ok = entry_sender == "*" or entry_sender == normalized_sender
        if channel_ok and sender_ok:
            return SenderDecision(allowed=True, reason="matched_allowlist", matched_entry=original)

    return SenderDecision(allowed=False, reason="sender_not_allowed")


def enforce_sender_allowed(*, channel: str, sender_id: str, allowed_senders: str | None = None) -> SenderDecision:
    decision = check_sender_allowed(channel=channel, sender_id=sender_id, allowed_senders=allowed_senders)
    if not decision.allowed:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="sender is not allowed to use inbound control",
            hint="Set OM_FEISHU_BOT_USER_OPEN_ID or OM_FEISHU_BOT_ALLOWED_OPEN_IDS.",
            details=decision.public_payload(),
        )
    return decision


def enforce_tool_allowed(call: InboundToolCall) -> dict[str, Any]:
    name = str(call.tool_name or "").strip()
    definition = get_tool_definition(name)
    if definition is None:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unknown inbound tool: {name}",
        )
    if name not in PURE_READ_TOOLS:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"{name} is not allowed through inbound read-only control",
            hint="Only pure-read tools are enabled for inbound control.",
            details={"allowed_tools": sorted(PURE_READ_TOOLS)},
        )
    risk_level = definition.risk_level or ("local_write" if definition.side_effects else "read_only")
    if risk_level != "read_only" or definition.side_effects or definition.requires_confirm:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"{name} is not a pure-read inbound tool",
            details={
                "risk_level": risk_level,
                "side_effects": list(definition.side_effects),
                "requires_confirm": bool(definition.requires_confirm),
            },
        )
    return {
        "allowed": True,
        "tool_name": name,
        "risk_level": risk_level,
        "reason": "pure_read_whitelist",
    }


def _default_allowed_senders(channel: str) -> str:
    if channel == "feishu":
        allowed = _parse_open_ids(os.environ.get("OM_FEISHU_BOT_ALLOWED_OPEN_IDS"))
        if not allowed:
            allowed = _parse_open_ids(os.environ.get("OM_FEISHU_BOT_USER_OPEN_ID"))
        return ",".join(f"feishu:{item}" for item in allowed)
    return ""


def _parse_open_ids(value: str | None) -> list[str]:
    out: list[str] = []
    for raw in str(value or "").split(","):
        item = raw.strip()
        if item and item not in out:
            out.append(item)
    return out


def _parse_allowed_entries(raw: str | None) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for piece in str(raw or "").replace("\n", ",").replace(";", ",").split(","):
        original = piece.strip()
        if not original:
            continue
        if ":" in original:
            channel, sender = original.split(":", 1)
        else:
            channel, sender = "*", original
        channel = channel.strip().lower() or "*"
        sender = sender.strip()
        if sender:
            out.append((channel, sender, original))
    return out


def _truthy_env(name: str, explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    value = str(os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}
