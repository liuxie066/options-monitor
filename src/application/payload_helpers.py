"""Shared payload-parsing helpers for src/application.

Consolidates the private ``_dict`` / ``_first_text`` / ``_optional_text`` /
``_required_text`` / ``_positive_int`` / ``_sha256`` copies that were
re-implemented per module. Pure stdlib; safe to import from anywhere in
``src/``. Callers bind private aliases, e.g.::

    from src.application.payload_helpers import as_dict as _dict
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def first_text(*values: Any, default: str | None = None) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def required_text(
    value: Any,
    field: str,
    *,
    error: Callable[[str], Exception] = ValueError,
) -> str:
    text = str(value or "").strip()
    if not text:
        raise error(f"{field} is required")
    return text


def positive_int_or(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed > 0 else default


def text_sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "as_dict",
    "first_text",
    "optional_text",
    "positive_int_or",
    "required_text",
    "text_sha256",
]
