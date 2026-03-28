#!/usr/bin/env python3
from __future__ import annotations

"""Normalization helpers for OpenD (futu-api) market data.

Why:
- OpenD returns implied volatility in different units depending on API/market.
  In practice we observe values like 164.036 / 273.263 which are clearly percent (%), not decimal.
- Downstream scanners assume IV is a decimal (e.g. 0.25 for 25%).

We keep normalization logic in a single place so both dev/prod stay consistent.
"""

import math


def normalize_iv(iv: float | None) -> float | None:
    """Normalize implied volatility to decimal.

    Accepts:
    - None -> None
    - 0 -> 0
    - 0.25 -> 0.25
    - 25 -> 0.25
    - 250 -> 2.5

    Heuristic:
    - If iv > 3.0, treat as percent.

    NOTE: We do NOT clamp aggressively here; scanners may want to filter via config.
    """
    try:
        if iv is None:
            return None
        v = float(iv)
        if math.isnan(v):
            return None
        if v < 0:
            return None
        if v > 3.0:
            v = v / 100.0
        return v
    except Exception:
        return None
