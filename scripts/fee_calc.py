"""Futu option fee calculation helpers."""

from __future__ import annotations

import pandas as pd


def _require_positive(name: str, value: float | None) -> float:
    if value is None or value <= 0:
        raise ValueError(f"{name} must be > 0")
    return float(value)


def calc_futu_us_option_fee(
    order_price: float,
    *,
    contracts: int = 1,
    multiplier: int = 100,
    is_sell: bool = True,
) -> float:
    """富途美股期权费用完整口径。"""
    price = _require_positive("order_price", float(order_price))
    qty = int(contracts)
    if qty <= 0:
        raise ValueError("contracts must be > 0")
    unit_multiplier = int(multiplier)
    if unit_multiplier <= 0:
        raise ValueError("multiplier must be > 0")

    transaction_amount = price * unit_multiplier * qty
    commission_per_contract = 0.65 if price > 0.1 else 0.15
    commission = max(commission_per_contract * qty, 1.99)
    platform_fee = 0.30 * qty
    orf = 0.02915 * qty
    sec_fee = max(transaction_amount * 0.0000229, 0.01) if is_sell else 0.0
    taf = max(0.00279 * qty, 0.01) if is_sell else 0.0
    total = commission + platform_fee + orf + sec_fee + taf
    return round(total, 6)


def calc_futu_hk_option_fee(
    order_price: float,
    *,
    contracts: int = 1,
    multiplier: int = 100,
    is_sell: bool = True,
) -> float:
    """富途港股期权费用完整口径。"""
    del is_sell
    price = _require_positive("order_price", float(order_price))
    qty = int(contracts)
    if qty <= 0:
        raise ValueError("contracts must be > 0")
    unit_multiplier = int(multiplier)
    if unit_multiplier <= 0:
        raise ValueError("multiplier must be > 0")

    transaction_amount = price * unit_multiplier * qty
    commission = max(transaction_amount * 0.002, 3.0)
    platform_fee = 15.0
    system_fee = 3.0 * qty
    total = commission + platform_fee + system_fee
    return round(total, 6)


def calc_futu_option_fee(
    currency: str | None,
    order_price: float,
    *,
    contracts: int = 1,
    multiplier: int = 100,
    is_sell: bool = True,
) -> float:
    ccy = str(currency or "USD").strip().upper()
    if ccy == "HKD":
        return calc_futu_hk_option_fee(
            order_price,
            contracts=contracts,
            multiplier=multiplier,
            is_sell=is_sell,
        )
    return calc_futu_us_option_fee(
        order_price,
        contracts=contracts,
        multiplier=multiplier,
        is_sell=is_sell,
    )


def safe_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def safe_int(v):
    try:
        if pd.isna(v):
            return None
        return int(float(v))
    except Exception:
        return None
