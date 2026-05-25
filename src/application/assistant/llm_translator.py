from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.application.assistant.commands import llm_capability_prompt
from src.application.assistant.llm_intent_schema import inbound_intent_from_llm_payload, llm_intent_json_schema, llm_intent_schema
from src.application.assistant.llm_common import (
    CreateStructuredResponseFn,
    llm_api_key_value,
    missing_llm_config,
    provider_create_response_fn,
    strip_json_code_fence,
)
from src.application.assistant.settings import LlmTranslatorSettings
from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.contracts import AssistantIntent
from src.infrastructure.openai_chat_completions import (
    OpenAIChatCompletionsError,
    extract_chat_completion_text,
)
from src.infrastructure.openai_responses import OpenAIResponsesError, extract_response_text


@dataclass(frozen=True)
class LlmTranslationResult:
    intent: AssistantIntent | None
    trace: dict[str, Any]
    error: AgentToolError | None = None


_TRANSLATOR_INSTRUCTIONS = """\
You translate one user message for options-monitor into one read-only intent.
Return only the requested JSON schema.

Rules:
- Never execute tools or claim an action was done.
- Only choose the read-only intents present in the schema.
- The input may be plain text or a JSON object with `message` and bounded, redacted `context`; translate the current `message`.
- Context is a hint only. The current message wins whenever it explicitly names an account, month, run id, or status.
- Do not translate write/admin actions such as recording trades, changing monitored symbols, upgrades, or confirmations.
- Use null for unknown optional arguments.
- For unclear or unsupported messages, return a low confidence value below 0.5.
- Use account only when the user explicitly mentions lx or sy.
- Use month only when the user explicitly mentions a YYYY-MM month.
- The JSON `intent` field is an OM capability_id.
- You can only choose capabilities marked llm_executable=true in the manifest below.
- If the user asks for a known but non-executable capability, such as writing trades, confirming operations, editing monitored symbols, or upgrading software, return confidence below 0.5.

Example JSON output:
{
  "schema_version": "om-llm-intent-v1",
  "intent": "runtime_status",
  "arguments": {
    "account": null,
    "status": null,
    "month": null,
    "run_id": null,
    "kind": null,
    "limit": null,
    "lines": null
  },
  "confidence": 0.91
}
"""


def parse_llm_translation_payload(
    payload: dict[str, Any],
    *,
    settings: LlmTranslatorSettings,
) -> LlmTranslationResult:
    try:
        intent = inbound_intent_from_llm_payload(payload, settings=settings)
    except AgentToolError as err:
        return LlmTranslationResult(
            intent=None,
            trace=_trace(settings, attempted=True, reason="invalid_payload", error_code=err.code),
            error=err,
        )
    return LlmTranslationResult(
        intent=intent,
        trace=_trace(settings, attempted=True, reason="accepted", schema_version=llm_intent_schema()["schema_version"]),
    )


def translate_inbound_intent(
    text: str,
    *,
    settings: LlmTranslatorSettings,
    environ: dict[str, str] | None = None,
    create_response_fn: CreateStructuredResponseFn | None = None,
    conversation_context: dict[str, Any] | None = None,
) -> LlmTranslationResult:
    if not settings.enabled:
        return LlmTranslationResult(
            intent=None,
            trace=_trace(settings, attempted=False, reason="disabled"),
        )

    missing = missing_llm_config(settings)
    if missing:
        return LlmTranslationResult(
            intent=None,
            trace=_trace(settings, attempted=False, reason="missing_config", missing=missing),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM translator is enabled but not fully configured.",
                hint="Set assistant.llm.provider, assistant.llm.model, and assistant.llm.api_key_env, or use assistant.mode=deterministic.",
                details={"missing": missing},
            ),
        )

    provider = str(settings.provider or "").strip().lower()
    if provider not in {"openai", "deepseek"}:
        return LlmTranslationResult(
            intent=None,
            trace=_trace(settings, attempted=False, reason="unsupported_provider"),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message=f"unsupported LLM translator provider: {settings.provider}",
                hint="Set assistant.llm.provider to openai or deepseek, or use assistant.mode=deterministic.",
                details={"provider": settings.provider},
            ),
        )

    api_key = llm_api_key_value(settings, environ=environ)
    if not api_key:
        return LlmTranslationResult(
            intent=None,
            trace=_trace(settings, attempted=False, reason="missing_api_key", missing=["api_key"]),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM translator API key is not configured.",
                hint=f"Set {settings.api_key_env} in the local env file or process environment.",
                details={"api_key_env": settings.api_key_env},
            ),
        )

    try:
        response = (create_response_fn or provider_create_response_fn(provider))(
            api_key=api_key,
            base_url=settings.base_url,
            model=settings.model,
            input_text=_provider_input_text(text, conversation_context=conversation_context),
            instructions=_translator_instructions(),
            json_schema=llm_intent_json_schema(),
            timeout=int(settings.timeout_seconds),
            max_output_tokens=int(settings.max_output_tokens),
        )
    except (OpenAIResponsesError, OpenAIChatCompletionsError) as err:
        return LlmTranslationResult(
            intent=None,
            trace=_trace(
                settings,
                attempted=True,
                reason="provider_error",
                error_code="LLM_PROVIDER_ERROR",
                conversation_context=conversation_context,
            ),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=str(err),
                details={"provider": provider, "http_status": err.http_status},
            ),
        )
    except Exception as err:
        return LlmTranslationResult(
            intent=None,
            trace=_trace(
                settings,
                attempted=True,
                reason="provider_error",
                error_code="LLM_PROVIDER_ERROR",
                conversation_context=conversation_context,
            ),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=f"LLM translator provider failed: {type(err).__name__}: {err}",
                details={"provider": provider},
            ),
        )

    payload = _parse_provider_payload(response)
    if payload is None:
        return LlmTranslationResult(
            intent=None,
            trace=_trace(
                settings,
                attempted=True,
                reason="invalid_provider_output",
                error_code="LLM_PROVIDER_ERROR",
                conversation_context=conversation_context,
            ),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message="LLM translator returned invalid JSON.",
                details={"provider": provider},
            ),
        )
    parsed = parse_llm_translation_payload(payload, settings=settings)
    parsed.trace["context"] = _context_trace(conversation_context)
    return parsed


def skipped_llm_trace(settings: LlmTranslatorSettings, *, reason: str) -> dict[str, Any]:
    return _trace(settings, attempted=False, reason=reason)


def _translator_instructions() -> str:
    return f"{_TRANSLATOR_INSTRUCTIONS}\n\n{llm_capability_prompt()}"


def _provider_input_text(text: str, *, conversation_context: dict[str, Any] | None) -> str:
    if not isinstance(conversation_context, dict):
        return str(text or "")
    return json.dumps(
        {
            "message": str(text or ""),
            "context": _provider_context(conversation_context),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _provider_context(conversation_context: dict[str, Any]) -> dict[str, Any]:
    recent = conversation_context.get("recent_messages")
    pending = conversation_context.get("pending_operations")
    return {
        "window_messages": int(conversation_context.get("window_messages") or 0),
        "semantics": conversation_context.get("semantics") if isinstance(conversation_context.get("semantics"), dict) else {},
        "last_successful_read": conversation_context.get("last_successful_read")
        if isinstance(conversation_context.get("last_successful_read"), dict)
        else None,
        "recent_messages": list(recent) if isinstance(recent, list) else [],
        "pending_operations": [_pending_operation_provider_item(item) for item in pending if isinstance(item, dict)]
        if isinstance(pending, list)
        else [],
    }


def _pending_operation_provider_item(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": operation.get("operation_id"),
        "operation_type": operation.get("operation_type"),
        "summary": operation.get("summary"),
        "status": operation.get("status"),
        "created_at": operation.get("created_at"),
        "expires_at": operation.get("expires_at"),
    }


def _context_trace(conversation_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(conversation_context, dict):
        return {"provided": False}
    recent = conversation_context.get("recent_messages")
    pending = conversation_context.get("pending_operations")
    return {
        "provided": True,
        "window_messages": int(conversation_context.get("window_messages") or 0),
        "recent_count": len(recent) if isinstance(recent, list) else 0,
        "pending_count": len(pending) if isinstance(pending, list) else 0,
    }


def _parse_provider_payload(response: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    text = extract_response_text(response) or extract_chat_completion_text(response)
    if not text:
        return None
    try:
        parsed = json.loads(strip_json_code_fence(text))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _trace(
    settings: LlmTranslatorSettings,
    *,
    attempted: bool,
    reason: str,
    missing: list[str] | None = None,
    error_code: str | None = None,
    schema_version: str | None = None,
    conversation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enabled": bool(settings.enabled),
        "attempted": bool(attempted),
        "reason": str(reason),
        "provider": settings.provider,
        "base_url": settings.base_url,
        "model": settings.model,
        "api_key_env": settings.api_key_env,
        "confidence_min": float(settings.confidence_min),
        "timeout_seconds": int(settings.timeout_seconds),
        "max_output_tokens": int(settings.max_output_tokens),
    }
    if missing:
        payload["missing"] = list(missing)
    if error_code:
        payload["error_code"] = str(error_code)
    if schema_version:
        payload["schema_version"] = str(schema_version)
    if conversation_context is not None:
        payload["context"] = _context_trace(conversation_context)
    return payload
