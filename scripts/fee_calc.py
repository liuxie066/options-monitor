"""Futu option fee calculation helpers (shared between sell_put and sell_call scanners)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def calc_futu_us_option_fee(order_price: float, contracts: int = 1, is_sell: bool = True) -> float:
    """富途美股单腿期权费用简化模型。"""
    commission_per_contract = 0.65 if order_price > 0.1 else 0.15
    commission = max(commission_per_contract * contracts, 1.99)
    platform_fee = 0.30 * contracts
    taf = max(0.00329 * contracts, 0.01) if is_sell else 0.0
    orf = 0.013 * contracts
    occ = 0.02 * contracts
    settlement = 0.18 * contracts
    total = commission + platform_fee + taf + orf + occ + settlement
    return round(total, 6)


def calc_futu_hk_option_fee_static(order_price: float, contracts: int = 1, is_sell: bool = True, *, base_dir: Path | None = None) -> float:
    """港股期权固定费用模型（HKD）。"""
    platform_fee_per_order = 15.0
    commission_per_order = 0.0
    other_per_order = 0.0

    try:
        if base_dir is not None:
            import json

            cfg = None
            for cfg_name in ("config.hk.json", "config.us.json"):
                cfg_path = base_dir / cfg_name
                if cfg_path.exists() and cfg_path.stat().st_size > 0:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    break
            if isinstance(cfg, dict):
                hk = ((cfg.get("fees") or {}).get("hk_static") or {})
                platform_fee_per_order = float(hk.get("platform_fee_per_order_hkd", platform_fee_per_order))
                commission_per_order = float(hk.get("commission_per_order_hkd", commission_per_order))
                other_per_order = float(hk.get("other_fees_per_order_hkd", other_per_order))
    except Exception:
        pass

    return round(platform_fee_per_order + commission_per_order + other_per_order, 6)


def calc_futu_option_fee(currency: str | None, order_price: float, contracts: int = 1, is_sell: bool = True, *, base_dir: Path | None = None) -> float:
    ccy = (currency or "USD").upper()
    if ccy == "HKD":
        return calc_futu_hk_option_fee_static(order_price, contracts=contracts, is_sell=is_sell, base_dir=base_dir)
    return calc_futu_us_option_fee(order_price, contracts=contracts, is_sell=is_sell)


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
