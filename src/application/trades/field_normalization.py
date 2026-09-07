from __future__ import annotations

from typing import Any

from domain.domain.trade_execution import canonical_decimal


def normalize_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_optional_int(value: Any) -> int | None:
    try:
        normalized = canonical_decimal(str(value) if isinstance(value, float) else value)
        if normalized is None or "." in normalized:
            return None
        return int(normalized)
    except (ValueError, TypeError):
        return None


def normalize_optional_float(value: Any) -> float | None:
    try:
        normalized = canonical_decimal(str(value) if isinstance(value, float) else value)
        if normalized is None:
            return None
        result = float(normalized)
        return result if result not in (float("inf"), float("-inf")) else None
    except (ValueError, TypeError, OverflowError):
        return None
