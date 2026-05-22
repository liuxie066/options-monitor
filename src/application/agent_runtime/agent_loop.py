from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.application.agent_runtime.llm_translator import LlmTranslationResult, translate_inbound_intent
from src.application.agent_runtime.settings import AgentRuntimeSettings


@dataclass(frozen=True)
class AgentLoopResult:
    translation: LlmTranslationResult
    trace: dict[str, Any]


AgentLoopTranslateFn = Callable[[str, AgentRuntimeSettings, dict[str, Any] | None], LlmTranslationResult]


def run_read_only_agent_loop(
    text: str,
    *,
    settings: AgentRuntimeSettings,
    conversation_context: dict[str, Any] | None,
    translate_intent_fn: AgentLoopTranslateFn | None = None,
    max_steps: int = 2,
) -> AgentLoopResult:
    """Bounded first implementation of the agent loop.

    The loop can only produce a structured read-only intent. Execution remains
    outside the loop and still goes through inbound routing and tool policy.
    """
    steps = max(1, min(int(max_steps), 4))
    translator = translate_intent_fn or _default_translate
    translation = translator(text, settings, conversation_context)
    trace = dict(translation.trace)
    trace["agent_loop"] = {
        "enabled": True,
        "planner": "llm_read_only_intent",
        "max_steps": steps,
        "steps_used": 1,
        "writes_allowed": False,
    }
    return AgentLoopResult(translation=translation, trace=trace)


def _default_translate(
    text: str,
    settings: AgentRuntimeSettings,
    conversation_context: dict[str, Any] | None,
) -> LlmTranslationResult:
    return translate_inbound_intent(text, settings=settings.llm, conversation_context=conversation_context)


__all__ = ["AgentLoopResult", "AgentLoopTranslateFn", "run_read_only_agent_loop"]
