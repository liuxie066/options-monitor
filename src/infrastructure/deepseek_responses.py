from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_DEEPSEEK_RESPONSES_URL = "https://api.deepseek.com/responses"
DEEPSEEK_WEB_SEARCH_TOOL = {"type": "web_search"}

HttpPostJsonFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class DeepSeekResponsesError(Exception):
    message: str
    http_status: int | None = None
    response: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def create_deepseek_response(
    *,
    api_key: str,
    model: str,
    input_items: list[dict[str, Any]],
    instructions: str,
    base_url: str | None = None,
    enable_web_search: bool = False,
    json_schema: dict[str, Any] | None = None,
    timeout: int = 60,
    max_output_tokens: int = 4096,
    http_post_json_fn: HttpPostJsonFn | None = None,
) -> dict[str, Any]:
    """Call the DeepSeek Responses API with optional native web_search.

    Returns the raw response payload. Callers use ``extract_output_text`` and
    ``extract_usage``; raw payloads are persisted by the owning audit layer.
    """

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
        "max_output_tokens": int(max_output_tokens),
        "store": False,
        "temperature": 0.0,
    }
    if enable_web_search:
        payload["tools"] = [dict(DEEPSEEK_WEB_SEARCH_TOOL)]
    if json_schema is not None:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": str(json_schema.get("name") or "structured_output"),
                "schema": dict(json_schema.get("schema") or {}),
                "strict": True,
            }
        }
    return (http_post_json_fn or _post_json)(
        resolve_deepseek_responses_url(base_url),
        payload,
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
        },
        timeout=int(timeout),
    )


def resolve_deepseek_responses_url(base_url: str | None) -> str:
    value = str(base_url or "").strip()
    if not value:
        return DEFAULT_DEEPSEEK_RESPONSES_URL
    normalized = value.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"


def extract_output_text(response: dict[str, Any]) -> str:
    """Concatenate output_text parts from a Responses payload."""

    parts: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text") or ""))
    return "".join(parts)


def extract_usage(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage")
    return dict(usage) if isinstance(usage, dict) else {}


def extract_web_search_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return native web_search_call audit records, if present."""

    calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            calls.append(dict(item))
    return calls


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
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
                raise DeepSeekResponsesError(
                    "invalid DeepSeek JSON response",
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
        message = _error_message(response) or f"DeepSeek API HTTP error {getattr(exc, 'code', None)}"
        raise DeepSeekResponsesError(message, http_status=getattr(exc, "code", None), response=response) from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise DeepSeekResponsesError(
            f"DeepSeek API network error: {type(exc).__name__}: {exc}",
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


def _error_message(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            return str(message)
    return None
