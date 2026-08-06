"""Exchange-rate loader.

Stage 3 refactor: keep per-symbol orchestration thin.

This wraps the legacy rate-cache reading into a single helper.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway
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
    """Fetch one FX observation from the canonical shared OpenD quote route."""

    route = resolve_shared_futu_quote_route(configs)
    if not route.ok or route.host is None or route.port is None:
        return None
    gateway = build_ready_futu_quote_gateway(
        host=route.host,
        port=route.port,
        is_option_chain_cache_enabled=False,
    )
    try:
        from src.application.futu_portfolio_context import (
            build_opend_exchange_rate_observation,
        )

        payload = gateway.get_snapshot(("FX.USDCNH", "FX.USDHKD"))
        rows = payload.to_dict("records") if hasattr(payload, "to_dict") else payload
        return build_opend_exchange_rate_observation(
            [dict(item) for item in rows if isinstance(item, Mapping)]
            if isinstance(rows, (list, tuple))
            else []
        )
    finally:
        gateway.close()
