"""Exchange-rate loader.

Stage 3 refactor: keep per-symbol orchestration thin.

This wraps the legacy rate-cache reading into a single helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from src.application.runtime_paths import resolve_runtime_root
from src.infrastructure.exchange_rates import (
    current_exchange_rate_snapshot,
    shared_exchange_rate_cache_path,
    CurrencyConverter,
    ExchangeRates,
    get_exchange_rates_or_fetch_latest,
)


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


def fetch_opend_exchange_rate_observation(
    configs: Iterable[tuple[str | None, Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """Fetch one FX observation through the market providers (Tencent/Sina).

    Renamed for compatibility; the OpenD derivation is retired as unreliable.
    The ``configs`` argument is accepted and ignored — the market FX source
    does not need an OpenD route.
    """

    del configs
    return get_exchange_rates_or_fetch_latest(
        cache_path=shared_exchange_rate_cache_path(resolve_runtime_root(repo_root=Path(__file__).resolve().parents[2]).runtime_root),
    )


def load_current_exchange_rate_snapshot(*, runtime_root: Path, write_cache: bool = False) -> dict[str, Any]:
    """Current facts for explicitly bound instance consumers, including Wheel."""
    return current_exchange_rate_snapshot(
        cache_path=shared_exchange_rate_cache_path(runtime_root), write_cache=write_cache,
    )
