from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

HttpPostJsonFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class OpenAIResponsesError(Exception):
    message: str
    http_status: int | None = None
    response: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def create_structured_response(
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
        "input": str(input_text or ""),
        "max_output_tokens": int(max_output_tokens),
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "om_llm_intent",
                "schema": dict(json_schema),
                "strict": True,
            }
        },
    }
    if temperature is not None:
        payload["temperature"] = float(temperature)
    return (http_post_json_fn or _post_json)(
        resolve_responses_url(base_url),
        payload,
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
        },
        timeout=int(timeout),
    )


def create_tool_call_response(
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
        "input": str(input_text or ""),
        "tools": list(tools or []),
        "tool_choice": "auto",
        "max_output_tokens": int(max_output_tokens),
        "store": False,
    }
    if temperature is not None:
        payload["temperature"] = float(temperature)
    return (http_post_json_fn or _post_json)(
        resolve_responses_url(base_url),
        payload,
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
        },
        timeout=int(timeout),
    )


def resolve_responses_url(base_url: str | None) -> str:
    value = str(base_url or "").strip()
    if not value:
        return DEFAULT_OPENAI_RESPONSES_URL
    normalized = value.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"


def extract_response_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


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
                raise OpenAIResponsesError(
                    "invalid OpenAI JSON response",
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
        message = _openai_error_message(response) or f"OpenAI API HTTP error {getattr(exc, 'code', None)}"
        raise OpenAIResponsesError(message, http_status=getattr(exc, "code", None), response=response) from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise OpenAIResponsesError(
            f"OpenAI API network error: {type(exc).__name__}: {exc}",
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


def _openai_error_message(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    if isinstance(error, dict) and str(error.get("message") or "").strip():
        return str(error.get("message")).strip()
    return None
