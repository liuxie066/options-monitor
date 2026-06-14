from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.capability_catalog import is_llm_planner_preview_spec, spec_by_intent
from src.application.assistant.contracts import AssistantRequest, ToolCall
from src.application.assistant.tool_policy import DEFAULT_TOOL_POLICY, ToolPolicyEngine


ACTION_POLICY_SCHEMA_VERSION = "om-agent-action-policy-v1"
_COMMAND_SPECS_BY_INTENT = spec_by_intent()


@dataclass(frozen=True)
class ActionPolicyDecision:
    allowed: bool
    decision: str
    tool_name: str
    risk_level: str
    allowed_effect: str
    reason: str
    source: str
    authority: str
    requires_confirmation: bool = False
    denied_reason: str | None = None
    apply_allowed: bool = False
    schema_version: str = ACTION_POLICY_SCHEMA_VERSION
    error: AgentToolError | None = field(default=None, repr=False, compare=False)

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "allowed": bool(self.allowed),
            "decision": self.decision,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level,
            "allowed_effect": self.allowed_effect,
            "reason": self.reason,
            "source": self.source,
            "authority": self.authority,
            "requires_confirmation": bool(self.requires_confirmation),
            "apply_allowed": bool(self.apply_allowed),
            "denied_reason": self.denied_reason,
        }


def decide_tool_action_policy(
    *,
    call: ToolCall,
    request: AssistantRequest | None,
    task_contract: dict[str, Any] | None = None,
    source: str = "agent_loop",
    tool_policy: ToolPolicyEngine = DEFAULT_TOOL_POLICY,
) -> ActionPolicyDecision:
    """Authorize one AgentLoop tool call.

    This is the P1 action-policy wrapper around the existing authorities. It
    allows read tools through ToolPolicyEngine and planner-approved preview
    capabilities only as preview operations. Apply/confirm paths still belong
    to deterministic operation handlers.
    """
    _ = request
    _ = task_contract
    tool_name = str(call.tool_name or "").strip()
    spec = _COMMAND_SPECS_BY_INTENT.get(tool_name)
    if spec is not None and is_llm_planner_preview_spec(spec):
        return ActionPolicyDecision(
            allowed=True,
            decision="allow_preview",
            tool_name=tool_name,
            risk_level=spec.risk_level or "preview_write",
            allowed_effect="preview",
            reason="planner_preview_capability",
            source=source,
            authority="capability_catalog.is_llm_planner_preview_spec",
            requires_confirmation=True,
            apply_allowed=False,
        )

    authority = "ToolPolicyEngine.authorize_read_tool"
    try:
        decision = tool_policy.authorize_read_tool(call, source=source)
    except AgentToolError as err:
        return ActionPolicyDecision(
            allowed=False,
            decision="deny",
            tool_name=tool_name,
            risk_level="unknown",
            allowed_effect="none",
            reason=str(err.code or "PERMISSION_DENIED"),
            source=source,
            authority=authority,
            denied_reason=str(err.message or ""),
            requires_confirmation=False,
            apply_allowed=False,
            error=err,
        )
    return ActionPolicyDecision(
        allowed=True,
        decision="allow_read",
        tool_name=tool_name,
        risk_level=decision.risk_level,
        allowed_effect="read",
        reason=decision.reason,
        source=source,
        authority=authority,
        requires_confirmation=False,
        apply_allowed=False,
    )


__all__ = [
    "ACTION_POLICY_SCHEMA_VERSION",
    "ActionPolicyDecision",
    "decide_tool_action_policy",
]
