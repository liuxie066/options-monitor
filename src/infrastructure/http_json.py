"""Shared HTTP JSON POST helper for ``src/infrastructure`` adapters.

Consolidates the byte-identical ``_post_json`` copies (plus their private
``_decode_body`` / ``_try_parse_json`` / provider error-message helpers) that
were re-implemented per OpenAI provider module. What differs per provider is
only the error class raised and the wording of its messages, so callers bind
those explicitly, e.g.::

    from src.infrastructure.http_json import post_json

    def _post_json(url, payload, *, headers=None, tool_choice="auto", timeout=20):
        return post_json(
            url,
            payload,
            headers=headers,
            timeout=timeout,
            error_cls=OpenAIResponsesError,
            error_label="OpenAI API",
            invalid_response_message="invalid OpenAI JSON response",
        )
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    error_cls: Callable[..., Exception],
    error_label: str,
    invalid_response_message: str,
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
                raise error_cls(
                    invalid_response_message,
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
        message = _error_message(response) or f"{error_label} HTTP error {getattr(exc, 'code', None)}"
        raise error_cls(message, http_status=getattr(exc, "code", None), response=response) from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise error_cls(
            f"{error_label} network error: {type(exc).__name__}: {exc}",
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
    if isinstance(error, dict) and str(error.get("message") or "").strip():
        return str(error.get("message")).strip()
    return None
