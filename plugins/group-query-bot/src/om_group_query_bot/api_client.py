"""Fixed loopback client for the five business routes.

The model never supplies the URL, method path, bearer token, request identity
or deadline: the host builds every one of them.  Redirects and oversized
responses are rejected so the dedicated token cannot be replayed elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import REQUEST_SCHEMA, RESPONSE_SCHEMA, ROUTES

MAX_RESPONSE_BYTES = 1024 * 1024


class QueryAPIError(RuntimeError):
    """A controlled business-API failure; no token or body is echoed."""

    def __init__(self, code: str, message: str, *, status: int | None = None,
                 retry_after_seconds: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _validate_base_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.port is None or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("query API base URL must be an explicit http://127.0.0.1:<port> URL")
    return f"http://127.0.0.1:{parsed.port}"


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("an aware UTC timestamp is required")
    return value.astimezone(timezone.utc).isoformat()


class QueryAPIClient:
    def __init__(self, *, base_url: str, token: str, timeout_seconds: float = 30,
                 opener=None):
        self.base_url = _validate_base_url(base_url)
        if not token or token != token.strip():
            raise ValueError("query API token is required")
        self._token = token
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("query API timeout must be positive")
        self._opener = opener or build_opener(_NoRedirect)

    def route(self, method: str) -> str:
        try:
            suffix, _capability = ROUTES[method]
        except KeyError as exc:
            raise QueryAPIError("NOT_FOUND", "unsupported group-query method") from exc
        return self.base_url + "/api/group-query-bot/v1" + suffix

    def call(self, *, method: str, request_id: str, deadline_utc: datetime,
             context: dict[str, Any], parameters: dict[str, Any],
             input_id: str | None = None, input_as_of_utc: datetime | None = None,
             timeout_seconds: float | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": REQUEST_SCHEMA,
            "request_id": request_id,
            "request_deadline_utc": _utc_text(deadline_utc),
            "request_context": dict(context),
            "parameters": dict(parameters),
        }
        if method in {"evaluate_close", "evaluate_roll_down", "evaluate_roll_up"}:
            if input_id is None or input_as_of_utc is None:
                raise QueryAPIError("INTERNAL_ERROR",
                                    "scenario input identity is required")
            body["input_id"] = input_id
            body["input_as_of_utc"] = _utc_text(input_as_of_utc)
        timeout = self.timeout_seconds
        if timeout_seconds is not None:
            timeout = min(timeout, float(timeout_seconds))
        if timeout <= 0:
            raise QueryAPIError("UPSTREAM_TIMEOUT", "request deadline has already elapsed")
        request = Request(
            self.route(method),
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token}"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(getattr(response, "status", 0) or 0)
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1) if exc.fp is not None else b""
            status = int(exc.code or 0)
            envelope = self._decode(raw)
            error = envelope.get("error") if isinstance(envelope, dict) else None
            code = str(error.get("code") or "DEPENDENCY_UNAVAILABLE") if isinstance(error, dict) else "DEPENDENCY_UNAVAILABLE"
            message = str(error.get("message") or "query API rejected the request") if isinstance(error, dict) else "query API rejected the request"
            retry = getattr(exc, "headers", None)
            retry_after = None
            if retry is not None:
                try:
                    retry_after = max(0, int(retry.get("Retry-After") or 0))
                except (TypeError, ValueError):
                    retry_after = None
            raise QueryAPIError(code, message, status=status,
                                retry_after_seconds=retry_after) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise QueryAPIError("DEPENDENCY_UNAVAILABLE",
                                "query API is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise QueryAPIError("INTERNAL_ERROR", "query API response is oversized")
        envelope = self._decode(raw)
        if not isinstance(envelope, dict):
            raise QueryAPIError("INTERNAL_ERROR", "query API response is malformed")
        if envelope.get("schema_version") != RESPONSE_SCHEMA:
            raise QueryAPIError("INTERNAL_ERROR", "query API response schema mismatch")
        if envelope.get("request_id") != request_id:
            raise QueryAPIError("INTERNAL_ERROR", "query API response identity mismatch")
        if envelope.get("ok") is not True:
            error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
            raise QueryAPIError(str(error.get("code") or "INTERNAL_ERROR"),
                                str(error.get("message") or "query API rejected the request"),
                                status=status)
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise QueryAPIError("INTERNAL_ERROR", "query API returned no data object")
        return data

    @staticmethod
    def _decode(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None


__all__ = ["MAX_RESPONSE_BYTES", "QueryAPIClient", "QueryAPIError"]
