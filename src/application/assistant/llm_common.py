from __future__ import annotations

from typing import Any, Callable

from src.application.assistant.settings import LlmTranslatorSettings
from src.application.settings import build_effective_env
from src.infrastructure.openai_chat_completions import create_json_chat_completion
from src.infrastructure.openai_responses import create_structured_response


CreateStructuredResponseFn = Callable[..., dict[str, Any]]


def provider_create_response_fn(provider: str) -> CreateStructuredResponseFn:
    if str(provider or "").strip().lower() == "deepseek":
        return create_json_chat_completion
    return create_structured_response


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
