from __future__ import annotations

from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.llm_provider_registry import (
    is_supported_llm_provider,
    normalize_llm_provider,
    provider_api_kind,
    supported_llm_providers,
)
from src.application.assistant.settings import LlmTranslatorSettings
from src.application.settings import build_effective_env
from src.infrastructure.openai_chat_completions import create_json_chat_completion
from src.infrastructure.openai_chat_completions import resolve_chat_completions_url
from src.infrastructure.openai_responses import create_structured_response
from src.infrastructure.openai_responses import resolve_responses_url


CreateStructuredResponseFn = Callable[..., dict[str, Any]]


def unsupported_llm_provider_error(settings: LlmTranslatorSettings, *, component: str) -> AgentToolError:
    providers = ", ".join(supported_llm_providers())
    return AgentToolError(
        code="LLM_UNAVAILABLE",
        message=f"unsupported LLM {component} provider: {settings.provider}",
        hint=f"Set assistant.llm.provider to one of: {providers}, or use assistant.mode=deterministic.",
        details={"provider": settings.provider, "supported_providers": list(supported_llm_providers())},
    )


def provider_create_response_fn(provider: str) -> CreateStructuredResponseFn:
    if normalize_llm_provider(provider) == "deepseek":
        return create_json_chat_completion
    return create_structured_response


def provider_endpoint_url(settings: LlmTranslatorSettings) -> str:
    return (
        resolve_chat_completions_url(settings.base_url)
        if provider_api_kind(settings.provider) == "chat_completions"
        else resolve_responses_url(settings.base_url)
    )


def strip_json_code_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def missing_llm_config(settings: LlmTranslatorSettings) -> list[str]:
    missing: list[str] = []
    if not str(settings.provider or "").strip():
        missing.append("provider")
    if not str(settings.model or "").strip():
        missing.append("model")
    if not str(settings.api_key_env or "").strip():
        missing.append("api_key_env")
    return missing


def llm_api_key_value(settings: LlmTranslatorSettings, *, environ: dict[str, str] | None) -> str:
    env = build_effective_env(environ=environ).values
    return str(env.get(settings.api_key_env) or "").strip()
