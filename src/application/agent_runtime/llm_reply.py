from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from src.application.agent_runtime.settings import LlmTranslatorSettings
from src.application.agent_tool_contracts import AgentToolError
from src.application.settings import build_effective_env
from src.infrastructure.openai_chat_completions import (
    OpenAIChatCompletionsError,
    create_json_chat_completion,
    extract_chat_completion_text,
)
from src.infrastructure.openai_responses import OpenAIResponsesError, create_structured_response, extract_response_text


@dataclass(frozen=True)
class LlmReplyResult:
    response_text: str | None
    trace: dict[str, Any]
    error: AgentToolError | None = None


CreateStructuredResponseFn = Callable[..., dict[str, Any]]

_GENERAL_REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
    },
    "required": ["reply"],
}

_GENERAL_REPLY_INSTRUCTIONS = """\
You are the conversational fallback for options-monitor.
Return only the requested JSON schema.

Rules:
- Reply in concise Chinese.
- You may answer harmless meta questions about what the assistant is, what it can do, or which configured LLM provider/model is being used.
- You may handle greetings and casual non-business chat briefly.
- Do not execute tools, claim an action was done, or produce portfolio, income, position, risk, market, ledger, service, or configuration facts.
- Do not provide trading advice or financial recommendations.
- If the user asks for OM data or an action, tell them to use /help or a specific supported command instead of inventing an answer.
- Never confirm, cancel, write, upgrade, restart, edit config, record trades, or change monitored symbols.
"""


def generate_general_reply(
    text: str,
    *,
    settings: LlmTranslatorSettings,
    environ: dict[str, str] | None = None,
    create_response_fn: CreateStructuredResponseFn | None = None,
    conversation_context: dict[str, Any] | None = None,
) -> LlmReplyResult:
    if not settings.enabled:
        return LlmReplyResult(
            response_text=None,
            trace=_trace(settings, attempted=False, reason="disabled"),
        )

    missing = _missing_config(settings)
    if missing:
        return LlmReplyResult(
            response_text=None,
            trace=_trace(settings, attempted=False, reason="missing_config", missing=missing),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM reply is enabled but not fully configured.",
                hint="Set assistant.llm.provider, assistant.llm.model, and assistant.llm.api_key_env, or use assistant.mode=deterministic.",
                details={"missing": missing},
            ),
        )

    provider = str(settings.provider or "").strip().lower()
    if provider not in {"openai", "deepseek"}:
        return LlmReplyResult(
            response_text=None,
            trace=_trace(settings, attempted=False, reason="unsupported_provider"),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message=f"unsupported LLM reply provider: {settings.provider}",
                hint="Set assistant.llm.provider to openai or deepseek, or use assistant.mode=deterministic.",
                details={"provider": settings.provider},
            ),
        )

    api_key = _api_key_value(settings, environ=environ)
    if not api_key:
        return LlmReplyResult(
            response_text=None,
            trace=_trace(settings, attempted=False, reason="missing_api_key", missing=["api_key"]),
            error=AgentToolError(
                code="LLM_UNAVAILABLE",
                message="LLM reply API key is not configured.",
                hint=f"Set {settings.api_key_env} in the local env file or process environment.",
                details={"api_key_env": settings.api_key_env},
            ),
        )

    try:
        response = (create_response_fn or _provider_create_response_fn(provider))(
            api_key=api_key,
            base_url=settings.base_url,
            model=settings.model,
            input_text=_provider_input_text(text, settings=settings, conversation_context=conversation_context),
            instructions=_GENERAL_REPLY_INSTRUCTIONS,
            json_schema=_GENERAL_REPLY_SCHEMA,
            timeout=int(settings.timeout_seconds),
            max_output_tokens=int(settings.max_output_tokens),
        )
    except (OpenAIResponsesError, OpenAIChatCompletionsError) as err:
        return LlmReplyResult(
            response_text=None,
            trace=_trace(settings, attempted=True, reason="provider_error", error_code="LLM_PROVIDER_ERROR"),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=str(err),
                details={"provider": provider, "http_status": err.http_status},
            ),
        )
    except Exception as err:
        return LlmReplyResult(
            response_text=None,
            trace=_trace(settings, attempted=True, reason="provider_error", error_code="LLM_PROVIDER_ERROR"),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message=f"LLM reply provider failed: {type(err).__name__}: {err}",
                details={"provider": provider},
            ),
        )

    reply = _parse_provider_reply(response)
    if not reply:
        return LlmReplyResult(
            response_text=None,
            trace=_trace(settings, attempted=True, reason="invalid_provider_output", error_code="LLM_PROVIDER_ERROR"),
            error=AgentToolError(
                code="LLM_PROVIDER_ERROR",
                message="LLM reply returned invalid JSON.",
                details={"provider": provider},
            ),
        )
    return LlmReplyResult(
        response_text=reply,
        trace=_trace(settings, attempted=True, reason="general_reply", schema_version="om-llm-reply-v1"),
    )


def _provider_create_response_fn(provider: str) -> CreateStructuredResponseFn:
    if str(provider or "").strip().lower() == "deepseek":
        return create_json_chat_completion
    return create_structured_response


def _provider_input_text(
    text: str,
    *,
    settings: LlmTranslatorSettings,
    conversation_context: dict[str, Any] | None,
) -> str:
    payload: dict[str, Any] = {
        "message": str(text or ""),
        "assistant": {
            "provider": settings.provider,
            "model": settings.model,
        },
    }
    if isinstance(conversation_context, dict):
        payload["context"] = {
            "window_messages": int(conversation_context.get("window_messages") or 0),
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _parse_provider_reply(response: dict[str, Any]) -> str | None:
    if not isinstance(response, dict):
        return None
    text = extract_response_text(response) or extract_chat_completion_text(response)
    if not text:
        return None
    try:
        parsed = json.loads(_strip_json_code_fence(text))
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    reply = str(parsed.get("reply") or "").strip()
    return reply or None


def _strip_json_code_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _missing_config(settings: LlmTranslatorSettings) -> list[str]:
    missing: list[str] = []
    if not str(settings.provider or "").strip():
        missing.append("provider")
    if not str(settings.model or "").strip():
        missing.append("model")
    if not str(settings.api_key_env or "").strip():
        missing.append("api_key_env")
    return missing


def _api_key_value(settings: LlmTranslatorSettings, *, environ: dict[str, str] | None) -> str:
    env = build_effective_env(environ=environ).values
    return str(env.get(settings.api_key_env) or "").strip()


def _trace(
    settings: LlmTranslatorSettings,
    *,
    attempted: bool,
    reason: str,
    missing: list[str] | None = None,
    error_code: str | None = None,
    schema_version: str | None = None,
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
        "facts_source": "none",
        "tools_allowed": False,
        "writes_allowed": False,
    }
    if missing:
        payload["missing"] = list(missing)
    if error_code:
        payload["error_code"] = str(error_code)
    if schema_version:
        payload["schema_version"] = str(schema_version)
    return payload
