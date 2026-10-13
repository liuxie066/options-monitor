"""Signed query continuation primitives shared by capability and display adapters."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from src.application.ledger.api import decode_evidence_cursor, encode_evidence_cursor
from src.application.secret_store import INBOUND_OPERATION_HMAC_KEY, SecretError, resolve_secret


class CursorError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _key() -> str:
    try:
        secret = resolve_secret(INBOUND_OPERATION_HMAC_KEY)
    except SecretError:
        raise CursorError("continuation_unavailable") from None
    if not secret:
        raise CursorError("continuation_unavailable")
    return hmac.new(secret.encode(), b"options-monitor/project-files/v1", hashlib.sha256).hexdigest()


def decode_cursor(cursor: str | None, binding: str) -> dict[str, Any] | None:
    if not cursor:
        return None
    if not isinstance(cursor, str) or len(cursor) > 8192:
        raise CursorError("cursor_invalidated")
    try:
        state = decode_evidence_cursor(cursor, _key())
    except CursorError:
        raise
    except ValueError:
        raise CursorError("cursor_invalidated") from None
    if state.get("binding") != binding:
        raise CursorError("cursor_invalidated")
    return state


def encode_cursor(state: dict[str, Any]) -> str:
    cursor = encode_evidence_cursor(state, _key())
    if len(cursor) > 8192:
        raise CursorError("scope_too_broad")
    return cursor
