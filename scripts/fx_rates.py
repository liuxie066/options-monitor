"""FX conversion utilities (Stage 2).

Goal: centralize FX math so that call-sites don't replicate USD/HKD/CNY conversions.

Conventions:
- fx_usd_per_cny: USD per 1 CNY (e.g., 0.14)
- hkdcny: CNY per 1 HKD (e.g., 0.92)

This module is intentionally minimal; expand only as needed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FxRates:
    usd_per_cny: float | None = None
    cny_per_hkd: float | None = None


@dataclass(frozen=True)
class CurrencyConverter:
    """Convert between base CNY and option native currencies (USD/HKD)."""

    rates: FxRates

    def cny_to_usd(self, cny: float) -> float | None:
        r = self.rates.usd_per_cny
        if r is None or r <= 0:
            return None
        return float(cny) * float(r)

    def usd_to_cny(self, usd: float) -> float | None:
        r = self.rates.usd_per_cny
        if r is None or r <= 0:
            return None
        return float(usd) / float(r)

    def cny_to_hkd(self, cny: float) -> float | None:
        # hkdcny is CNY per 1 HKD
        hkdcny = self.rates.cny_per_hkd
        if hkdcny is None or hkdcny <= 0:
            return None
        return float(cny) / float(hkdcny)

    def hkd_to_cny(self, hkd: float) -> float | None:
        hkdcny = self.rates.cny_per_hkd
        if hkdcny is None or hkdcny <= 0:
            return None
        return float(hkd) * float(hkdcny)

    def cny_to_native(self, cny: float, *, native_ccy: str) -> float | None:
        c = str(native_ccy or '').upper()
        if c == 'USD':
            return self.cny_to_usd(cny)
        if c == 'HKD':
            return self.cny_to_hkd(cny)
        return None

    def native_to_cny(self, amount: float, *, native_ccy: str) -> float | None:
        c = str(native_ccy or '').upper()
        if c == 'USD':
            return self.usd_to_cny(amount)
        if c == 'HKD':
            return self.hkd_to_cny(amount)
        if c == 'CNY':
            return float(amount)
        return None
