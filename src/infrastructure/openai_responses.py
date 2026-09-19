from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.infrastructure.http_json import post_json


DEFAULT_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

HttpPostJsonFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class OpenAIResponsesError(Exception):
    message: str
    http_status: int | None = None
    response: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def create_response(
    *,
    api_key: str,
    base_url: str | None = None,
    model: str,
    input_items: list[dict[str, Any]],
    instructions: str,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "auto",
    timeout: int = 20,
    max_output_tokens: int | None = None,
    temperature: float | None = 0.0,
    http_post_json_fn: HttpPostJsonFn | None = None,
) -> dict[str, Any]:
    api_key_value = str(api_key or "").strip()
    model_value = str(model or "").strip()
    if not api_key_value:
        raise ValueError("api_key is required")
    if not model_value:
        raise ValueError("model is required")
    payload: dict[str, Any] = {
        "model": model_value,
        "instructions": str(instructions or "").strip(),
        "input": [dict(item) for item in input_items],
        "store": False,
    }
    if max_output_tokens is not None:
        payload["max_output_tokens"] = int(max_output_tokens)
    if tools:
        payload["tools"] = [dict(item) for item in tools]
        payload["tool_choice"] = tool_choice
    if temperature is not None:
        payload["temperature"] = float(temperature)
    return (http_post_json_fn or _post_json)(
        resolve_responses_url(base_url),
        payload,
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
        },
        timeout=float(timeout),
    )


def resolve_responses_url(base_url: str | None) -> str:
    value = str(base_url or "").strip()
    if not value:
        return DEFAULT_OPENAI_RESPONSES_URL
    normalized = value.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"


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
        error_cls=OpenAIResponsesError,
        error_label="OpenAI API",
        invalid_response_message="invalid OpenAI JSON response",
    )
