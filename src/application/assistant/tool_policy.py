from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_registry import get_tool_definition
from src.application.assistant.contracts import ToolCall
from src.application.tool_allowlist import PURE_READ_TOOLS


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    tool_name: str
    risk_level: str
    reason: str
    source: str

    def public_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "source": self.source,
        }


class ToolPolicyEngine:
    def __init__(self, *, read_only_tools: set[str] | frozenset[str] | None = None) -> None:
        self._read_only_tools = frozenset(read_only_tools or PURE_READ_TOOLS)

    def authorize_read_tool(self, call: ToolCall, *, source: str) -> ToolPolicyDecision:
        name = str(call.tool_name or "").strip()
        definition = get_tool_definition(name)
        if definition is None:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"unknown inbound tool: {name}",
            )
        if name not in self._read_only_tools:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message=f"{name} is not allowed through read-only control",
                hint="Only pure-read tools are enabled for inbound/agent control.",
                details={"allowed_tools": sorted(self._read_only_tools), "source": source},
            )
        risk_level = definition.risk_level or ("local_write" if definition.side_effects else "read_only")
        if risk_level != "read_only" or definition.side_effects or definition.requires_confirm:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message=f"{name} is not a pure-read tool",
                details={
                    "risk_level": risk_level,
                    "side_effects": list(definition.side_effects),
                    "requires_confirm": bool(definition.requires_confirm),
                    "source": source,
                },
            )
        return ToolPolicyDecision(
            allowed=True,
            tool_name=name,
            risk_level=risk_level,
            reason="pure_read_whitelist",
            source=source,
        )


DEFAULT_TOOL_POLICY = ToolPolicyEngine()


__all__ = [
    "DEFAULT_TOOL_POLICY",
    "PURE_READ_TOOLS",
    "ToolPolicyDecision",
    "ToolPolicyEngine",
]
