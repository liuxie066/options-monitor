from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.application.copilot.contracts import SceneManifest


ActionDecider = Callable[["AgentState"], "AgentAction"]


@dataclass(frozen=True)
class AgentAction:
    kind: str
    tool_name: str | None = None
    reason: str = ""
    final_report: dict[str, Any] | None = None
    error_code: str | None = None


@dataclass
class AgentState:
    manifest: SceneManifest
    observations: list[dict[str, Any]] = field(default_factory=list)
    attempted_tools: list[str] = field(default_factory=list)
    tool_calls: int = 0
    turns: int = 0


def default_action_decider(state: AgentState) -> AgentAction:
    attempted = set(state.attempted_tools)
    for tool_name in state.manifest.allowed_tools:
        if tool_name not in attempted:
            return AgentAction(kind="tool", tool_name=tool_name, reason="next allowed read-only tool")
    return AgentAction(kind="finish", reason="all manifest tools attempted")
