"""FX loader.

Stage 3 refactor: keep per-symbol orchestration thin.

This wraps the legacy rate-cache reading into a single helper.
"""

from __future__ import annotations

from pathlib import Path

from scripts.fx_rates import CurrencyConverter, FxRates


def build_converter(*, fx_usd_per_cny: float | None, hkdcny: float | None) -> CurrencyConverter:
    return CurrencyConverter(FxRates(usd_per_cny=fx_usd_per_cny, cny_per_hkd=hkdcny))
