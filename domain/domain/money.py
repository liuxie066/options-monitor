from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


MONEY_QUANTUM = Decimal("0.000001")


def to_decimal(value: Any, *, field_name: str = "value") -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{field_name} is required")
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not out.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return out


def coerce_decimal(value: Any) -> Decimal:
    """Coerce a value to Decimal without validating finiteness.

    Non-finite inputs (NaN/Infinity) and unparseable strings are preserved as
    ``Decimal("NaN")`` so a deferred validator (e.g. ``validate_trade_event``)
    can report them as diagnostics instead of raising at construction.
    """
    if isinstance(value, bool):
        value = int(value)
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return Decimal("NaN")


def canonical_decimal_text(value: Any, *, field_name: str = "value") -> str:
    decimal_value = to_decimal(value, field_name=field_name)
    if decimal_value == 0:
        return "0"
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def quantize_money(value: Any) -> Decimal:
    return to_decimal(value, field_name="amount").quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


__all__ = [
    "MONEY_QUANTUM",
    "canonical_decimal_text",
    "coerce_decimal",
    "quantize_money",
    "to_decimal",
]
