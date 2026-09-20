"""Pandas-aware numeric coercion helpers for src/application.

Consolidates the byte-identical ``_float`` / ``_safe_float`` copies that were
re-implemented per module. Unlike ``src.application.payload_helpers`` this
module imports pandas, so import it only from call sites that already deal in
dataframe/Series values. Callers bind their own local name, e.g.::

    from src.application.numeric_helpers import float_or_none as _float
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        parsed = float(value)
    except Exception:
        return None
    try:
        if parsed != parsed:
            return None
    except Exception:
        pass
    return parsed


def safe_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return None


__all__ = [
    "float_or_none",
    "safe_float",
]
