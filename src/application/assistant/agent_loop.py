from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.application.assistant.llm_translator import LlmTranslationResult, translate_inbound_intent
from src.application.assistant.settings import AssistantSettings

AGENT_LOOP_SCHEMA_VERSION = "om-agent-loop-v1"


@dataclass(frozen=True)
class AgentLoopResult:
    translation: LlmTranslationResult
    trace: dict[str, Any]
    steps: tuple["AgentLoopStep", ...] = ()


@dataclass(frozen=True)
class AgentLoopStep:
    index: int
    phase: str
    status: str
    intent_name: str | None = None
    arguments: dict[str, Any] | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "phase": self.phase,
            "status": self.status,
            "intent_name": self.intent_name,
            "arguments": dict(self.arguments or {}),
        }


@dataclass(frozen=True)
class ToolObservation:
    index: int
    tool_name: str
    payload: dict[str, Any]
    ok: bool
    error_code: str | None
    summary: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "tool_name": self.tool_name,
            "payload": dict(self.payload),
            "ok": bool(self.ok),
            "error_code": self.error_code,
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class FinalResponsePlan:
    status: str
    reason: str
    canonical_renderer_required: bool = True
    llm_may_summarize: bool = False

    def public_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "canonical_renderer_required": bool(self.canonical_renderer_required),
            "llm_may_summarize": bool(self.llm_may_summarize),
        }


AgentLoopTranslateFn = Callable[[str, AssistantSettings, dict[str, Any] | None], LlmTranslationResult]


def run_read_only_agent_loop(
    text: str,
    *,
    settings: AssistantSettings,
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
    planned_steps = _planned_steps_from_translation(translation)
    trace = dict(translation.trace)
    trace["agent_loop"] = {
        "schema_version": AGENT_LOOP_SCHEMA_VERSION,
        "enabled": True,
        "planner": "llm_read_only_intent",
        "max_steps": steps,
        "steps_used": len(planned_steps),
        "writes_allowed": False,
        "steps": [step.public_payload() for step in planned_steps],
        "final_response": FinalResponsePlan(
            status="pending_tool_execution" if translation.intent is not None else "no_plan",
            reason="canonical renderer will own factual output"
            if translation.intent is not None
            else "translator did not produce an executable read-only intent",
        ).public_payload(),
    }
    return AgentLoopResult(translation=translation, trace=trace, steps=planned_steps)


def build_tool_observation(*, index: int, tool_name: str, payload: dict[str, Any], result: dict[str, Any]) -> ToolObservation:
    error = result.get("error") if isinstance(result, dict) else None
    error_code = error.get("code") if isinstance(error, dict) else None
    return ToolObservation(
        index=index,
        tool_name=str(tool_name or ""),
        payload=_safe_tool_payload(payload),
        ok=bool(result.get("ok", False)) if isinstance(result, dict) else False,
        error_code=str(error_code) if error_code else None,
        summary=_safe_result_summary(result),
    )


def _default_translate(
    text: str,
    settings: AssistantSettings,
    conversation_context: dict[str, Any] | None,
) -> LlmTranslationResult:
    return translate_inbound_intent(text, settings=settings.llm, conversation_context=conversation_context)


def _planned_steps_from_translation(translation: LlmTranslationResult) -> tuple[AgentLoopStep, ...]:
    if translation.intent is None:
        return ()
    return (
        AgentLoopStep(
            index=1,
            phase="plan_tool",
            status="planned",
            intent_name=translation.intent.intent_name,
            arguments=dict(translation.intent.arguments),
        ),
    )


def _safe_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"config_key", "account", "status", "month", "run_id", "kind", "limit", "lines", "action"}
    return {key: payload[key] for key in sorted(allowed) if key in payload}


def _safe_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    data = result.get("data")
    warnings = result.get("warnings")
    summary: dict[str, Any] = {
        "tool_name": result.get("tool_name"),
        "warning_count": len(warnings) if isinstance(warnings, list) else 0,
    }
    if isinstance(data, dict):
        if isinstance(data.get("summary"), dict):
            summary["summary"] = _clip_mapping(data["summary"])
        elif isinstance(data.get("summary"), list):
            summary["summary_count"] = len(data["summary"])
        if "response_text" in data:
            summary["response_text_chars"] = len(str(data.get("response_text") or ""))
        for key in ("status", "pending_count", "symbol_count"):
            if key in data:
                summary[key] = data[key]
    return {key: value for key, value in summary.items() if value is not None}


def _clip_mapping(value: dict[str, Any], *, limit: int = 12) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= limit:
            out["..."] = "truncated"
            break
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[str(key)] = item
        elif isinstance(item, list):
            out[str(key)] = {"type": "list", "count": len(item)}
        elif isinstance(item, dict):
            out[str(key)] = {"type": "object", "keys": sorted(str(k) for k in item.keys())[:limit]}
        else:
            out[str(key)] = {"type": type(item).__name__}
    return out


__all__ = [
    "AGENT_LOOP_SCHEMA_VERSION",
    "AgentLoopResult",
    "AgentLoopStep",
    "AgentLoopTranslateFn",
    "FinalResponsePlan",
    "ToolObservation",
    "build_tool_observation",
    "run_read_only_agent_loop",
]
