from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.application.copilot.contracts import SceneManifest


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    attempt_count: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    force_finish: bool = False
    timeout_seconds: int | None = None


ModelRunner = Callable[[ModelRequest], ModelTurn]


@dataclass
class AgentState:
    manifest: SceneManifest
    messages: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    call_signatures: set[str] = field(default_factory=set)
    call_attempts: dict[str, int] = field(default_factory=dict)
    call_outcomes: dict[str, str] = field(default_factory=dict)
    result_pages: dict[str, str] = field(default_factory=dict)
    accumulated_text_parts: list[str] = field(default_factory=list)
    tool_calls: int = 0
    iterations: int = 0
    continuation_count: int = 0
    compaction_count: int = 0
    model_retry_count: int = 0


@dataclass(frozen=True)
class AgentRunResult:
    status: str
    text: str = ""
    error: dict[str, Any] | None = None


__all__ = [
    "AgentRunResult",
    "AgentState",
    "ModelRequest",
    "ModelRunner",
    "ModelTurn",
    "ToolCall",
]
