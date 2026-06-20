from __future__ import annotations

import json
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.capability_catalog import is_llm_planner_preview_spec, spec_by_intent
from src.application.assistant.contracts import PerceptionResult


_COMMAND_SPECS_BY_INTENT = spec_by_intent()
_INTENT_KEYS = ("intent_name", "tool_name", "capability_id", "name")
_MANUAL_TRADE_PREVIEW_INTENTS = frozenset({"manual_trade_open", "manual_trade_close", "manual_assignment", "manual_expiry"})


def preview_request_perception_from_payload(
    preview_request: dict[str, Any],
    *,
    question: str,
    source: str = "agent_loop_events",
) -> PerceptionResult:
    payload = _preview_request_payload(preview_request)
    intent_name = _preview_request_intent_name(payload)
    spec = _COMMAND_SPECS_BY_INTENT.get(intent_name)
    if spec is None or not is_llm_planner_preview_spec(spec):
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"{intent_name or 'missing'} is not an allowed assistant preview capability.",
            hint="Model preview requests may only target planner-allowed preview capabilities; confirm/apply must use deterministic commands.",
            details={"intent_name": intent_name, "allowed_preview_intents": _allowed_preview_intents()},
        )

    arguments = _preview_request_arguments(payload)
    for key in spec.arguments:
        if key in payload and key not in arguments:
            arguments[key] = payload[key]
    if "account" in payload and "account" not in arguments:
        arguments["account"] = payload["account"]
    if intent_name in _MANUAL_TRADE_PREVIEW_INTENTS:
        raw_text = str(question or "").strip() or str(arguments.get("raw_text") or "").strip()
        if raw_text:
            arguments["raw_text"] = raw_text

    return PerceptionResult(
        intent_name=intent_name,
        arguments=arguments,
        source=source,
        confidence=1.0,
    )


def normalized_preview_request_payload(
    preview_request: dict[str, Any],
    *,
    question: str,
) -> dict[str, Any]:
    perception = preview_request_perception_from_payload(preview_request, question=question)
    return {
        "intent_name": perception.intent_name,
        "arguments": dict(perception.arguments),
    }


def _preview_request_payload(preview_request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(preview_request, dict):
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="preview_request must be an object")
    payload = preview_request.get("payload")
    if isinstance(payload, dict):
        return dict(payload)
    return dict(preview_request)


def _preview_request_intent_name(payload: dict[str, Any]) -> str:
    for key in _INTENT_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _preview_request_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("arguments")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentToolError(
                code="INVALID_MODEL_EVENT",
                message="preview_request arguments are not valid JSON",
                details={"error": str(exc)},
            ) from exc
        if not isinstance(parsed, dict):
            raise AgentToolError(code="INVALID_MODEL_EVENT", message="preview_request arguments must be an object")
        return dict(parsed)
    raise AgentToolError(code="INVALID_MODEL_EVENT", message="preview_request arguments must be an object")


def _allowed_preview_intents() -> list[str]:
    return sorted(
        spec.intent_name
        for spec in _COMMAND_SPECS_BY_INTENT.values()
        if is_llm_planner_preview_spec(spec)
    )


__all__ = ["normalized_preview_request_payload", "preview_request_perception_from_payload"]
