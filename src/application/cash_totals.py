"""Shared currency-to-CNY cash aggregation helpers."""

from __future__ import annotations


def sum_by_currency_to_cny(
    by_currency: dict,
    *,
    usdcny_exchange_rate: float | None,
    cny_per_hkd_exchange_rate: float | None,
) -> float | None:
    total = 0.0
    ok = True
    for ccy, v in (by_currency or {}).items():
        try:
            fv = float(v)
        except Exception:
            continue
        if not fv:
            continue
        c = str(ccy).strip().upper()
        if c in ('CNY', 'RMB'):
            total += fv
        elif c == 'USD':
            if not usdcny_exchange_rate:
                ok = False
                break
            total += fv * float(usdcny_exchange_rate)
        elif c == 'HKD':
            if not cny_per_hkd_exchange_rate:
                ok = False
                break
            total += fv * float(cny_per_hkd_exchange_rate)
        else:
            ok = False
            break
    return total if ok else None
