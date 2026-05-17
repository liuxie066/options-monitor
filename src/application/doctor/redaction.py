from __future__ import annotations

import re
from typing import Any


SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "password",
    "secret",
    "token",
    "webhook",
)

WEBHOOK_RE = re.compile(r"https?://[^\s\"']*(?:webhook|hook|bot|token|key)[^\s\"']*", re.IGNORECASE)
LONG_NUMBER_RE = re.compile(r"\b\d{10,}\b")
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        key_lower = key_text.lower()
        if any(part in key_lower for part in SECRET_KEY_PARTS):
            out[key_text] = "***REDACTED***"
            continue
        out[key_text] = redact_value(value)
    return out


def redact_text(text: str) -> str:
    out = WEBHOOK_RE.sub("***REDACTED_URL***", str(text))
    out = BEARER_RE.sub("Bearer ***REDACTED***", out)
    out = LONG_NUMBER_RE.sub(lambda match: f"...{match.group(0)[-4:]}", out)
    return out
