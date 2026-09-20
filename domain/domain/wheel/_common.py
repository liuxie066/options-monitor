"""Wheel schema constants and the numeric/market micro-helpers.

This module imports no sibling module; every other module in the package may
import from it.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from domain.domain.symbol_identity import symbol_market


WHEEL_EVENT_TYPES_V1 = frozenset(
    {
        "wheel_started",
        "wheel_manual_ended",
        "wheel_called_away",
        "wheel_call_intent_created",
        "wheel_call_intent_cancelled",
        "wheel_call_intent_consumed",
        "wheel_call_linkage_rejected",
        "wheel_event_voided",
    }
)
WHEEL_EVENT_TYPES = WHEEL_EVENT_TYPES_V1 | frozenset(
    {
        "wheel_branch_created",
        "wheel_branch_decided",
        "wheel_put_intent_created",
        "wheel_put_intent_cancelled",
        "wheel_put_intent_consumed",
        "wheel_put_linkage_rejected",
    }
)
WHEEL_EVENT_SCHEMA_V1 = "wheel_event.v1"
WHEEL_EVENT_SCHEMA_V2 = "wheel_event.v2"
WHEEL_EVENT_SCHEMA = WHEEL_EVENT_SCHEMA_V2
WHEEL_PROJECTION_SCHEMA = "wheel_projection.v2"


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _wheel_market(value: Mapping[str, Any] | str) -> str:
    symbol = value.get("symbol") if isinstance(value, Mapping) else value
    market = str(symbol_market(symbol) or "").strip().lower()
    declared = (
        str(value.get("market") or "").strip().lower()
        if isinstance(value, Mapping)
        else ""
    )
    if market not in {"us", "hk"} or declared not in {"", market}:
        raise ValueError("Wheel symbol market is invalid")
    return market


def _wheel_abs_delta_bounds(
    wheel_policy: Mapping[str, Any],
) -> tuple[float | None, float | None]:
    minimum = _finite_float(wheel_policy.get("min_abs_delta", 0.25))
    maximum = _finite_float(wheel_policy.get("max_abs_delta", 0.35))
    if (
        minimum is None
        or maximum is None
        or minimum < 0
        or maximum > 1
        or minimum > maximum
    ):
        return None, None
    return minimum, maximum
