from __future__ import annotations

from datetime import date
from typing import Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.contracts import PerceptionResult


def parse_deterministic_text(text: str, *, now_fn: Callable[[], date] | None = None) -> PerceptionResult:
    """Backward-compatible protocol parser.

    Natural-language routing now belongs to AgentLoop. This compatibility entry
    only accepts explicit slash commands so old imports cannot silently re-enable
    keyword-based business routing.
    """
    raw = str(text or "").strip()
    if not raw:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="请输入 command，例如 /help。",
            hint="自然语言请求需要进入 AgentLoop；显式控制请使用 slash command。",
        )
    command_intent = parse_assistant_command(raw, now_fn=now_fn)
    if command_intent is not None:
        return command_intent
    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="自然语言请求不能通过 deterministic parser 处理。",
        hint="请使用 /help 查看显式 command，或通过 assistant runtime 进入 AgentLoop。",
    )


__all__ = ["parse_deterministic_text"]
