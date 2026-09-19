"""Shared payload-parsing helpers for src/application.

Consolidates the private ``_dict`` / ``_first_text`` / ``_optional_text`` /
``_required_text`` / ``_positive_int`` / ``_sha256`` copies that were
re-implemented per module. Pure stdlib; safe to import from anywhere in
``src/``. Callers bind private aliases, e.g.::

    from src.application.payload_helpers import as_dict as _dict

Also hosts the shared plain-text, value-coercion, timestamp-parsing and
canonical-JSON copies that were byte-identical per module (``_text``,
``_optional_id``, ``_parse_utc``, ``_parse_datetime``, ``_nested``,
``_as_float_or_none``, ``_positive_integer``, ``_canonical_bytes``,
``_json_bytes``, ``_config_bool``, ``_config_positive_int``,
``_float_setting``, ``_float_setting_from_sources``,
``_optional_float_setting``). Wall-clock "now" helpers live in
``src.infrastructure.io_utils.utc_now``; pandas-aware numeric coercion lives
in ``src.application.numeric_helpers``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def text(value: Any) -> str:
    return str(value or "").strip()


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


def as_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def nested(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    if not numeric.is_finite() or parsed <= 0 or numeric != parsed:
        return None
    return parsed


def config_bool(explicit: bool | None, configured: Any, *, default: bool) -> bool:
    if explicit is not None:
        return bool(explicit)
    if isinstance(configured, bool):
        return configured
    if configured is None:
        return bool(default)
    value = str(configured or "").strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def config_positive_int(explicit: int | None, configured: Any, *, default: int) -> int:
    raw = explicit if explicit is not None else configured
    if raw is None or str(raw).strip() == "":
        raw = default
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(1, value)


def config_float(raw: dict[str, Any], key: str, default: float) -> float:
    try:
        value = raw.get(key, default)
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def config_float_from_sources(key: str, default: float, *sources: dict[str, Any]) -> float:
    for source in sources:
        if not isinstance(source, dict) or key not in source:
            continue
        return config_float(source, key, default)
    return float(default)


def config_optional_float(raw: dict[str, Any], key: str) -> float | None:
    try:
        value = raw.get(key)
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_bytes_lines(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def readable_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "as_dict",
    "as_float_or_none",
    "canonical_json_bytes",
    "canonical_json_bytes_lines",
    "config_bool",
    "config_float",
    "config_float_from_sources",
    "config_optional_float",
    "config_positive_int",
    "first_text",
    "nested",
    "optional_text",
    "parse_utc",
    "positive_int_or",
    "positive_integer",
    "readable_json_bytes",
    "required_text",
    "text",
    "text_sha256",
]
