from __future__ import annotations

import json
import hashlib
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
    response_sha256: str | None = None

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

    Returns the response payload to the immediate caller. Callers must extract
    only the validated output and the minimized audit fields below; provider
    response bodies are never an audit or persistence contract.
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
    """Return an allowlisted, numeric-only usage summary."""

    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result


def response_fingerprint(response: dict[str, Any]) -> str:
    """Fingerprint a provider response without retaining its contents."""

    encoded = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def summarize_web_search_calls(response: dict[str, Any]) -> dict[str, Any]:
    """Return aggregate web-search telemetry without provider IDs or queries."""

    count = 0
    status_counts: dict[str, int] = {}
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            count += 1
            raw_status = str(item.get("status") or "").strip().lower()
            status = raw_status if raw_status in {"completed", "failed", "in_progress"} else "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "count": count,
        "status_counts": dict(sorted(status_counts.items())),
    }


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
                    response_sha256=hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                )
            return parsed
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = _decode_body(exc.read())
        except Exception:
            body_text = ""
        raise DeepSeekResponsesError(
            f"DeepSeek API HTTP error {getattr(exc, 'code', None)}",
            http_status=getattr(exc, "code", None),
            response_sha256=hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        ) from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise DeepSeekResponsesError(
            f"DeepSeek API network error: {type(exc).__name__}",
            http_status=None,
        ) from exc


def _decode_body(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None
