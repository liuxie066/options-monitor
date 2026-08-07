"""Exchange-rate loader.

Stage 3 refactor: keep per-symbol orchestration thin.

This wraps the legacy rate-cache reading into a single helper.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
from src.application.futu_quote_routing import resolve_shared_futu_quote_route


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
    """Fetch one FX observation through the canonical shared OpenD route."""

    config_items = tuple(configs)
    route = resolve_shared_futu_quote_route(config_items)
    if not route.ok or route.host is None or route.port is None:
        return None
    from src.application.futu_portfolio_context import (
        fetch_futu_exchange_rate_observation,
    )

    attempted = False
    last_error: Exception | None = None
    for account, config in sorted(
        config_items,
        key=lambda item: str(item[0] or ""),
    ):
        account_norm = str(account or "").strip().lower()
        if not account_norm:
            continue
        attempted = True
        try:
            observation = fetch_futu_exchange_rate_observation(
                cfg=config,
                account=account_norm,
            )
        except Exception as exc:
            last_error = exc
            continue
        if observation is not None:
            return observation
    if attempted and last_error is not None:
        raise last_error
    return None
