"""Shared currency-to-CNY cash aggregation helpers."""

from __future__ import annotations

import math

from domain.domain.option_position_identity import normalize_currency


def sum_by_currency_to_cny(
    by_currency: dict,
    *,
    usdcny_exchange_rate: float | None,
    cny_per_hkd_exchange_rate: float | None,
) -> float | None:
    total = 0.0
    for ccy, v in (by_currency or {}).items():
        if isinstance(v, bool):
            return None
        try:
            fv = float(v)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(fv):
            return None
        if not fv:
            continue
        rate = {"CNY": 1, "USD": usdcny_exchange_rate, "HKD": cny_per_hkd_exchange_rate}.get(normalize_currency(ccy))
        if isinstance(rate, bool):
            return None
        try:
            rate = float(rate)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(rate) or rate <= 0:
            return None
        total += fv * rate
    return total if math.isfinite(total) else None
