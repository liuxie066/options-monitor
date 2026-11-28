"""Signed continuation tokens; callers own query scope and key selection."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Mapping

CURSOR_TTL_SECONDS = 30 * 60


class SignedCursorError(ValueError):
    """A safe, explicit validation failure for signed continuations."""

    def __init__(self, message: str, *, code: str = "invalid_cursor"):
        self.code = code
        super().__init__(message)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def encode_cursor(
    state: Mapping[str, Any],
    key: str,
    *,
    now: int | None = None,
) -> str:
    secret = str(key or "").strip()
    if not secret:
        raise SignedCursorError(
            "cursor signing key is unavailable",
            code="cursor_key_unavailable",
        )
    issued = int(time.time() if now is None else now)
    body = dict(state)
    body["iat"] = issued
    body["exp"] = issued + CURSOR_TTL_SECONDS
    raw = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def decode_cursor(
    cursor: str,
    key: str,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    secret = str(key or "").strip()
    if not secret:
        raise SignedCursorError(
            "cursor signing key is unavailable",
            code="cursor_key_unavailable",
        )
    try:
        encoded, encoded_signature = str(cursor).split(".", 1)
        raw = _unb64(encoded)
        supplied_signature = _unb64(encoded_signature)
        expected_signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise SignedCursorError(
                "invalid cursor signature",
                code="invalid_cursor_signature",
            )
        state = json.loads(raw)
    except SignedCursorError:
        raise
    except Exception as exc:
        raise SignedCursorError(
            "invalid cursor",
            code="invalid_cursor",
        ) from exc
    if not isinstance(state, dict):
        raise SignedCursorError(
            "invalid cursor state",
            code="invalid_cursor",
        )

    current = int(time.time() if now is None else now)
    issued = _required_int(state, "iat")
    expires = _required_int(state, "exp")
    if issued > current:
        raise SignedCursorError(
            "cursor issued-at time is in the future",
            code="invalid_cursor",
        )
    if current >= expires:
        raise SignedCursorError(
            "cursor expired; start a new query (new results may overlap)",
            code="cursor_expired",
        )
    return state


def _required_int(state: Mapping[str, Any], field: str) -> int:
    value = state.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _cursor_mismatch(f"cursor {field} is invalid")
    return int(value)


def _cursor_mismatch(message: str) -> SignedCursorError:
    return SignedCursorError(message, code="invalid_cursor")
