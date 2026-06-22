from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


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


def create_json_chat_completion(
    *,
    api_key: str,
    base_url: str | None = None,
    model: str,
    input_text: str,
    instructions: str,
    json_schema: dict[str, Any],
    timeout: int = 20,
    max_output_tokens: int = 512,
    temperature: float | None = 0.0,
    thinking: dict[str, Any] | None = DEFAULT_CHAT_COMPLETIONS_THINKING,
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
        "messages": [
            {
                "role": "system",
                "content": _system_content(instructions=instructions, json_schema=json_schema),
            },
            {"role": "user", "content": str(input_text or "")},
        ],
        "max_tokens": int(max_output_tokens),
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    if thinking is not None:
        payload["thinking"] = dict(thinking)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    return (http_post_json_fn or _post_json)(
        resolve_chat_completions_url(base_url),
        payload,
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
        },
        timeout=int(timeout),
    )


def create_tool_call_chat_completion(
    *,
    api_key: str,
    base_url: str | None = None,
    model: str,
    input_text: str,
    instructions: str,
    tools: list[dict[str, Any]],
    timeout: int = 20,
    max_output_tokens: int = 512,
    temperature: float | None = 0.0,
    thinking: dict[str, Any] | None = DEFAULT_CHAT_COMPLETIONS_THINKING,
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
        "messages": [
            {"role": "system", "content": str(instructions or "").strip()},
            {"role": "user", "content": str(input_text or "")},
        ],
        "tools": list(tools or []),
        "tool_choice": "auto",
        "max_tokens": int(max_output_tokens),
        "stream": False,
    }
    if thinking is not None:
        payload["thinking"] = dict(thinking)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    return (http_post_json_fn or _post_json)(
        resolve_chat_completions_url(base_url),
        payload,
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
        },
        timeout=int(timeout),
    )


def create_chat_completion_from_payload(
    *,
    api_key: str,
    payload: dict[str, Any],
    base_url: str | None = None,
    timeout: int = 20,
    http_post_json_fn: HttpPostJsonFn | None = None,
) -> dict[str, Any]:
    api_key_value = str(api_key or "").strip()
    if not api_key_value:
        raise ValueError("api_key is required")
    if not isinstance(payload, dict):
        raise ValueError("payload is required")

    return (http_post_json_fn or _post_json)(
        resolve_chat_completions_url(base_url),
        dict(payload),
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
        },
        timeout=int(timeout),
    )


def resolve_chat_completions_url(base_url: str | None) -> str:
    value = str(base_url or "").strip()
    if not value:
        return DEFAULT_DEEPSEEK_CHAT_COMPLETIONS_URL
    normalized = value.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def extract_chat_completion_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list):
        return ""
    chunks: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            chunks.append(content.strip())
    return "\n".join(chunks).strip()


def _system_content(*, instructions: str, json_schema: dict[str, Any]) -> str:
    return "\n\n".join(
        [
            str(instructions or "").strip(),
            "Return a single JSON object matching this JSON Schema:",
            json.dumps(dict(json_schema), ensure_ascii=False, sort_keys=True),
        ]
    ).strip()


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers=dict(headers or {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body_text = _decode_body(resp.read())
            parsed = _try_parse_json(body_text)
            if not isinstance(parsed, dict):
                raise OpenAIChatCompletionsError(
                    "invalid chat completions JSON response",
                    http_status=getattr(resp, "status", None),
                    response={"body": body_text},
                )
            return parsed
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = _decode_body(exc.read())
        except Exception:
            body_text = ""
        parsed = _try_parse_json(body_text)
        response = parsed if isinstance(parsed, dict) else {"body": body_text}
        message = _chat_completion_error_message(response) or f"chat completions API HTTP error {getattr(exc, 'code', None)}"
        raise OpenAIChatCompletionsError(message, http_status=getattr(exc, "code", None), response=response) from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise OpenAIChatCompletionsError(
            f"chat completions API network error: {type(exc).__name__}: {exc}",
            http_status=None,
            response={"error_type": type(exc).__name__, "error": str(exc)},
        ) from exc


def _decode_body(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _chat_completion_error_message(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    if isinstance(error, dict) and str(error.get("message") or "").strip():
        return str(error.get("message")).strip()
    return None
