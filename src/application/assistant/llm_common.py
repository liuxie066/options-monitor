from __future__ import annotations

from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.llm_provider_registry import (
    is_supported_llm_provider,
    normalize_llm_provider,
    provider_api_kind,
    provider_chat_completion_payload_options,
    supported_llm_providers,
)
from src.application.assistant.settings import AssistantLlmSettings
from src.application.settings import build_effective_env
from src.infrastructure.openai_chat_completions import (
    create_chat_completion_from_payload,
    create_json_chat_completion,
    create_tool_call_chat_completion,
)
from src.infrastructure.openai_chat_completions import resolve_chat_completions_url
from src.infrastructure.openai_responses import create_response_from_payload, create_structured_response, create_tool_call_response
from src.infrastructure.openai_responses import resolve_responses_url


CreateStructuredResponseFn = Callable[..., dict[str, Any]]
CreateToolCallResponseFn = Callable[..., dict[str, Any]]
CreateToolCallPayloadResponseFn = Callable[..., dict[str, Any]]


def unsupported_llm_provider_error(settings: AssistantLlmSettings, *, component: str) -> AgentToolError:
    providers = ", ".join(supported_llm_providers())
    return AgentToolError(
        code="LLM_UNAVAILABLE",
        message=f"unsupported LLM {component} provider: {settings.provider}",
        hint=f"Set assistant.llm.provider to one of: {providers}, or disable assistant.agent_loop.enabled.",
        details={"provider": settings.provider, "supported_providers": list(supported_llm_providers())},
    )


def provider_create_response_fn(provider: str) -> CreateStructuredResponseFn:
    normalized = normalize_llm_provider(provider)
    if provider_api_kind(normalized) == "chat_completions":
        return _chat_completions_response_fn(create_json_chat_completion, provider=normalized)
    return create_structured_response


def provider_create_tool_call_response_fn(provider: str) -> CreateToolCallResponseFn:
    normalized = normalize_llm_provider(provider)
    if provider_api_kind(normalized) == "chat_completions":
        return _chat_completions_response_fn(create_tool_call_chat_completion, provider=normalized)
    return create_tool_call_response


def provider_create_tool_call_payload_response_fn(provider: str) -> CreateToolCallPayloadResponseFn:
    if provider_api_kind(provider) == "chat_completions":
        return create_chat_completion_from_payload
    return create_response_from_payload


def provider_endpoint_url(settings: AssistantLlmSettings) -> str:
    return (
        resolve_chat_completions_url(settings.base_url)
        if provider_api_kind(settings.provider) == "chat_completions"
        else resolve_responses_url(settings.base_url)
    )


def chat_completions_payload_options(provider: str) -> dict[str, Any]:
    return provider_chat_completion_payload_options(provider)


def _chat_completions_response_fn(fn: Callable[..., dict[str, Any]], *, provider: str) -> Callable[..., dict[str, Any]]:
    options = provider_chat_completion_payload_options(provider)

    def _create(**kwargs: Any) -> dict[str, Any]:
        call_kwargs = dict(kwargs)
        for key, value in options.items():
            call_kwargs.setdefault(key, value)
        return fn(**call_kwargs)

    return _create


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


def missing_llm_config(settings: AssistantLlmSettings) -> list[str]:
    missing: list[str] = []
    if not str(settings.provider or "").strip():
        missing.append("provider")
    if not str(settings.model or "").strip():
        missing.append("model")
    if not str(settings.api_key_env or "").strip():
        missing.append("api_key_env")
    return missing


def llm_api_key_value(settings: AssistantLlmSettings, *, environ: dict[str, str] | None) -> str:
    env = build_effective_env(environ=environ).values
    return str(env.get(settings.api_key_env) or "").strip()
