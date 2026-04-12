from __future__ import annotations

from typing import Any


ERR_TIMEOUT = "ERR_TIMEOUT"
ERR_2FA_REQUIRED = "ERR_2FA_REQUIRED"
ERR_UPSTREAM_UNAVAILABLE = "ERR_UPSTREAM_UNAVAILABLE"
ERR_CONFIG = "ERR_CONFIG"
ERR_UNEXPECTED = "ERR_UNEXPECTED"


_TIMEOUT_HINTS = ("timeout", "timed out")
_TWO_FA_HINTS = ("2fa", "phone_verify", "needs_phone_verify", "phone verification", "need_2fa")
_UPSTREAM_UNAVAILABLE_CODES = {
    "OPEND_PORT_CLOSED",
    "OPEND_NOT_READY",
    "OPEND_QOT_NOT_LOGINED",
    "OPEND_API_ERROR",
    "OPEND_RATE_LIMIT",
    "TOOL_EXEC_FAILED",
}


def classify_failure(
    *,
    error_code: str | None = None,
    message: str | None = None,
    exc: BaseException | None = None,
    upstream: str | None = None,
) -> dict[str, Any]:
    code = str(error_code or "").strip().upper()
    msg = str(message or "").strip()
    exc_name = type(exc).__name__ if exc is not None else ""
    txt = f"{code} {msg} {exc_name}".lower()

    if any(hint in txt for hint in _TIMEOUT_HINTS):
        return {
            "error_code": ERR_TIMEOUT,
            "category": "timeout",
            "retryable": True,
            "fallback_allowed": False,
            "upstream": str(upstream or ""),
        }
    if (code == "OPEND_NEEDS_PHONE_VERIFY") or any(hint in txt for hint in _TWO_FA_HINTS):
        return {
            "error_code": ERR_2FA_REQUIRED,
            "category": "2fa",
            "retryable": False,
            "fallback_allowed": False,
            "upstream": str(upstream or ""),
        }
    if ("config" in txt):
        return {
            "error_code": ERR_CONFIG,
            "category": "config",
            "retryable": False,
            "fallback_allowed": False,
            "upstream": str(upstream or ""),
        }
    if code in _UPSTREAM_UNAVAILABLE_CODES:
        return {
            "error_code": ERR_UPSTREAM_UNAVAILABLE,
            "category": "upstream_unavailable",
            "retryable": True,
            "fallback_allowed": True,
            "upstream": str(upstream or ""),
        }
    if code:
        return {
            "error_code": ERR_UPSTREAM_UNAVAILABLE if upstream else code,
            "category": "upstream",
            "retryable": True,
            "fallback_allowed": True,
            "upstream": str(upstream or ""),
        }
    return {
        "error_code": ERR_UPSTREAM_UNAVAILABLE if upstream else ERR_UNEXPECTED,
        "category": ("upstream" if upstream else "unexpected"),
        "retryable": True,
        "fallback_allowed": True,
        "upstream": str(upstream or ""),
    }
