from __future__ import annotations

import re
from typing import Any


SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session",
    "signature",
    "signing",
    "token",
    "webhook",
)

SENSITIVE_ID_KEYS = frozenset(
    {
        "account_id",
        "app_id",
        "chat_id",
        "conversation_id",
        "event_id",
        "group_id",
        "message_id",
        "open_id",
        "operator_id",
        "recipient_id",
        "sender_id",
        "tenant_id",
        "to_user_id",
        "union_id",
        "user_id",
    }
)

WEBHOOK_RE = re.compile(r"https?://[^\s\"']*(?:webhook|hook|bot|token|key)[^\s\"']*", re.IGNORECASE)
LONG_NUMBER_RE = re.compile(r"\b\d{10,}\b")
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
BASIC_AUTH_RE = re.compile(r"\bBasic\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
AUTHORIZATION_HEADER_RE = re.compile(
    r"\b(?:Authorization|Proxy-Authorization)\s*:[^\r\n]*",
    re.IGNORECASE,
)
PEM_RE = re.compile(
    r"-----BEGIN [^-]*(?:PRIVATE KEY|SECRET)[^-]*-----.*?-----END [^-]*(?:PRIVATE KEY|SECRET)[^-]*-----",
    re.IGNORECASE | re.DOTALL,
)
KEY_VALUE_RE = re.compile(
    r"(?P<key>(?P<key_quote>[\"']?)\b(?:api[_-]?key|access[_-]?key|authorization|auth|bearer|client[_-]?secret|cookie|credential|password|private[_-]?key|refresh[_-]?token|secret|session(?:id)?|signature|signing[_-]?key|token|webhook)\b(?P=key_quote))"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"(?:\\[\s\S]|[^\"\\])*\"|'(?:\\[\s\S]|[^'\\])*'|[^\s,;\"'&]+)",
    re.IGNORECASE,
)
COOKIE_HEADER_RE = re.compile(
    r"\b(?:Cookie|Set-Cookie)\s*:[^\r\n]*",
    re.IGNORECASE,
)
QUERY_SECRET_RE = re.compile(
    r"(?P<prefix>[?&](?:access_token|api_key|key|secret|session|sig|signature|token)=)[^&#\s]+",
    re.IGNORECASE,
)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
PROVIDER_IDENTIFIER_RE = re.compile(
    r"\b(?:cli|oc|om|on|ou|ox|tenant|union|wxid)_[A-Za-z0-9_-]{4,}\b",
    re.IGNORECASE,
)
LOCAL_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<path>/(?:Applications|Library|Users|Volumes|etc|home|opt|private|root|run|srv|tmp|var)/[^'\"\n,}\s]+)"
)


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
        if key_lower in SENSITIVE_ID_KEYS:
            out[key_text] = "***REDACTED_ID***"
            continue
        if any(part in key_lower for part in SECRET_KEY_PARTS):
            out[key_text] = "***REDACTED***"
            continue
        out[key_text] = redact_value(value)
    return out


def redact_text(text: str, *, preserve_newlines: bool = False) -> str:
    """Redact known patterns, optionally preserving source line numbering."""
    def replace(pattern: re.Pattern[str], replacement: Any, value: str) -> str:
        def apply(match: re.Match[str]) -> str:
            result = replacement(match) if callable(replacement) else replacement
            if preserve_newlines:
                result += "\n***REDACTED***" * max(0, match.group().count("\n") - result.count("\n"))
            return result
        return pattern.sub(apply, value)

    out = str(text)
    for pattern, replacement in (
        (PEM_RE, "***REDACTED_PEM***"),
        (KEY_VALUE_RE, lambda m: f"{m.group('key')}{m.group('sep')}***REDACTED***"),
        (WEBHOOK_RE, "***REDACTED_URL***"),
        (AUTHORIZATION_HEADER_RE, "Authorization: ***REDACTED***"),
        (BEARER_RE, "Bearer ***REDACTED***"),
        (BASIC_AUTH_RE, "Basic ***REDACTED***"),
        (COOKIE_HEADER_RE, "Cookie: ***REDACTED***"),
        (QUERY_SECRET_RE, lambda m: f"{m.group('prefix')}***REDACTED***"),
        (JWT_RE, "***REDACTED_JWT***"),
        (PROVIDER_IDENTIFIER_RE, "***REDACTED_ID***"),
        (LONG_NUMBER_RE, lambda m: f"...{m.group(0)[-4:]}"),
        (LOCAL_ABSOLUTE_PATH_RE, _mask_local_path),
    ):
        out = replace(pattern, replacement, out)
    return out


def _mask_local_path(match: re.Match[str]) -> str:
    raw = match.group("path").rstrip(".:;)")
    suffix = match.group("path")[len(raw) :]
    name = raw.rsplit("/", 1)[-1]
    return (f".../{name}" if name else "...") + suffix
