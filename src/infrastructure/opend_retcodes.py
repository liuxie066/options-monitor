from __future__ import annotations

from enum import Enum
from typing import Any


class OpenDRetCode(str, Enum):
    OK = "OK"
    RATE_LIMIT = "RATE_LIMIT"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    NEED_2FA = "NEED_2FA"
    TRANSIENT = "TRANSIENT"
    EMPTY_CHAIN = "EMPTY_CHAIN"
    UNKNOWN = "UNKNOWN"

    @property
    def is_rate_limit(self) -> bool:
        return self is OpenDRetCode.RATE_LIMIT

    @property
    def is_retryable(self) -> bool:
        return self in (OpenDRetCode.RATE_LIMIT, OpenDRetCode.TRANSIENT)


_RATE_LIMIT_HINTS_LOW = ("rate limit", "too frequent")
_RATE_LIMIT_HINTS_TEXT = ("频率太高", "最多10次", "频率限制", "请求过快")
_AUTH_EXPIRED_HINTS_LOW = ("login expired", "auth expired", "token expired", "not logged", "not login")
_NEED_2FA_HINTS_LOW = ("2fa", "phone verification", "verify code")
_NEED_2FA_HINTS_TEXT = ("手机验证码", "短信验证", "手机验证", "验证码")
_TRANSIENT_HINTS_LOW = ("timeout", "disconnected", "connection reset", "broken pipe", "temporarily unavailable")
_EMPTY_CHAIN_HINTS_LOW = ("empty_chain", "empty")


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _from_code(raw: Any) -> OpenDRetCode | None:
    code = str(raw or "").strip().upper()
    if not code:
        return None
    try:
        return OpenDRetCode(code)
    except ValueError:
        return None


def _message_from_payload(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("message", "error"):
        value = payload.get(key)
        if str(value or "").strip():
            parts.append(str(value))
    return " ".join(parts).strip()


def _classify_message(message: str) -> OpenDRetCode:
    text = str(message or "").strip()
    if not text:
        return OpenDRetCode.UNKNOWN
    low = text.lower()
    if _contains_any(low, _NEED_2FA_HINTS_LOW) or _contains_any(text, _NEED_2FA_HINTS_TEXT):
        return OpenDRetCode.NEED_2FA
    if _contains_any(low, _AUTH_EXPIRED_HINTS_LOW):
        return OpenDRetCode.AUTH_EXPIRED
    if _contains_any(low, _RATE_LIMIT_HINTS_LOW) or _contains_any(text, _RATE_LIMIT_HINTS_TEXT):
        return OpenDRetCode.RATE_LIMIT
    if _contains_any(low, _TRANSIENT_HINTS_LOW):
        return OpenDRetCode.TRANSIENT
    if _contains_any(low, _EMPTY_CHAIN_HINTS_LOW):
        return OpenDRetCode.EMPTY_CHAIN
    return OpenDRetCode.UNKNOWN


def classify_opend_error(value: Any) -> OpenDRetCode:
    code_attr = _from_code(getattr(value, "code", None))
    if code_attr is not None:
        return code_attr

    if isinstance(value, dict):
        code = _from_code(value.get("error_code"))
        if code is not None:
            return code
        return _classify_message(_message_from_payload(value))

    if value in (None, ""):
        return OpenDRetCode.UNKNOWN
    return _classify_message(str(value))
