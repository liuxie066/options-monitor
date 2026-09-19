from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.infrastructure.http_json import post_json


DEFAULT_DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_CHAT_COMPLETIONS_THINKING = {"type": "disabled"}

HttpPostJsonFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class OpenAIChatCompletionsError(Exception):
    message: str
    http_status: int | None = None
    response: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def create_chat_completion(
    *,
    api_key: str = "",
    base_url: str | None = None,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "auto",
    timeout: int = 20,
    max_output_tokens: int | None = None,
    temperature: float | None = 0.0,
    thinking: dict[str, Any] | None = DEFAULT_CHAT_COMPLETIONS_THINKING,
    http_post_json_fn: HttpPostJsonFn | None = None,
) -> dict[str, Any]:
    api_key_value = str(api_key or "").strip()
    model_value = str(model or "").strip()
    if not model_value:
        raise ValueError("model is required")
    payload: dict[str, Any] = {
        "model": model_value,
        "messages": [dict(item) for item in messages],
        "stream": False,
    }
    if max_output_tokens is not None:
        payload["max_tokens"] = int(max_output_tokens)
    if tools:
        payload["tools"] = [dict(item) for item in tools]
        payload["tool_choice"] = tool_choice
    if thinking is not None:
        payload["thinking"] = dict(thinking)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    return (http_post_json_fn or _post_json)(
        resolve_chat_completions_url(base_url),
        payload,
        headers=_request_headers(api_key_value),
        timeout=float(timeout),
    )


def resolve_chat_completions_url(base_url: str | None) -> str:
    value = str(base_url or "").strip()
    if not value:
        return DEFAULT_DEEPSEEK_CHAT_COMPLETIONS_URL
    normalized = value.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _request_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    tool_choice: str = "auto",
    timeout: int = 20,
) -> dict[str, Any]:
    return post_json(
        url,
        payload,
        headers=headers,
        timeout=timeout,
        error_cls=OpenAIChatCompletionsError,
        error_label="chat completions API",
        invalid_response_message="invalid chat completions JSON response",
    )
