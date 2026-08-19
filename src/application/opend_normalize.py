#!/usr/bin/env python3
from __future__ import annotations

"""Normalization helpers for OpenD (futu-api) market data."""

import math
from typing import Any


def normalize_iv(iv: float | None) -> float | None:
    """Convert OpenD's percent-valued IV to the downstream decimal contract."""
    try:
        if iv is None:
            return None
        v = float(iv)
        if not math.isfinite(v):
            return None
        if v < 0:
            return None
        return v / 100.0
    except Exception:
        return None


def normalize_opend_option_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("put", "optiontype.put"):
        return "put"
    if raw in ("call", "optiontype.call"):
        return "call"
    return raw
