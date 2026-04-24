"""Exchange-rate loader.

Stage 3 refactor: keep per-symbol orchestration thin.

This wraps the legacy rate-cache reading into a single helper.
"""

from __future__ import annotations

from pathlib import Path

from scripts.exchange_rates import CurrencyConverter, ExchangeRates


def build_converter(
    *,
    usd_per_cny_exchange_rate: float | None,
    cny_per_hkd_exchange_rate: float | None,
) -> CurrencyConverter:
    return CurrencyConverter(
        ExchangeRates(
            usd_per_cny=usd_per_cny_exchange_rate,
            cny_per_hkd=cny_per_hkd_exchange_rate,
        )
    )
