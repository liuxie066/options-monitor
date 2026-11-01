"""Builtin market-data capability boundary; no provider discovery or live I/O on selection.

Implementations return the existing opening quote, underlier, expiration and
earnings evidence contracts unchanged. Provider choice never grants broker
account access or changes freshness, retry, cancellation or resource budgets.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class OpenDMarketDataProvider:
    name = "opend"

    def resolve_quote_route(self, config, **kwargs):
        from src.application.futu_quote_routing import resolve_futu_quote_route
        return resolve_futu_quote_route(config, **kwargs)

    def open_quote_gateway(self, **kwargs):
        from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway
        return build_ready_futu_quote_gateway(**kwargs)

    def get_pooled_quote_gateway(self, pool, **kwargs):
        return pool.get_gateway(**kwargs)

    def fetch_symbol_request(self, request, **kwargs):
        from src.application.opend_symbol_fetching import fetch_symbol_request
        return fetch_symbol_request(request, **kwargs)

    def get_underlier_observations(self, *args, **kwargs):
        from src.application.opend_market_snapshot_fetching import get_underlier_observations_opend
        return get_underlier_observations_opend(*args, **kwargs)

    def get_underlier_observation(self, *args, **kwargs):
        from src.application.opend_market_snapshot_fetching import get_underlier_observation
        return get_underlier_observation(*args, **kwargs)

    def get_stock_spot(self, *args, **kwargs):
        from src.application.opend_market_snapshot_fetching import get_spot_opend
        return get_spot_opend(*args, **kwargs)

    def get_trading_days_with_receipt(self, gateway, **kwargs):
        return gateway.get_trading_days_with_receipt(**kwargs)

    def get_market_state(self, gateway, codes):
        return gateway.get_market_state(codes)

    def discover_option_expirations(self, *args, **kwargs):
        from src.application.opend_symbol_chain_fetching import discover_option_expirations
        return discover_option_expirations(*args, **kwargs)

    def fetch_option_chains(self, **kwargs):
        from src.application.option_chain_fetching import fetch_option_chains
        return fetch_option_chains(**kwargs)

    def fetch_earnings_calendar(self, **kwargs):
        from src.application.earnings_calendar import fetch_market_earnings_calendar
        return fetch_market_earnings_calendar(**kwargs)

    def prefetch_earnings_calendars(self, **kwargs):
        from src.application.earnings_calendar import prefetch_market_earnings_calendars
        return prefetch_market_earnings_calendars(**kwargs)


# A fixed builtin table. Tests may replace it with isolated implementations.
_BUILTIN_PROVIDERS = {"opend": OpenDMarketDataProvider()}


def resolve_market_data_provider(config: Mapping[str, Any] | None = None):
    section = (config or {}).get("market_data", {})
    if not isinstance(section, Mapping):
        raise ValueError("market_data must be an object")
    name = section.get("provider", "opend")
    if not isinstance(name, str) or name not in _BUILTIN_PROVIDERS:
        raise ValueError(f"unsupported market_data.provider: {name!r}")
    return _BUILTIN_PROVIDERS[name]


def provider_by_name(name: str):
    return resolve_market_data_provider({"market_data": {"provider": name}})


def market_data_source(provider: str = "opend", legacy_source: str = "opend") -> str:
    """Keep historical OpenD bindings; other providers own their source identity."""
    name = provider_by_name(provider).name
    return legacy_source if name == "opend" else name


def fetch_symbol_request(request, **kwargs):
    return provider_by_name(request.market_data_provider).fetch_symbol_request(request, **kwargs)


def fetch_symbol(*args, market_data_provider="opend", **kwargs):
    from src.application.opend_symbol_fetching import fetch_symbol as build_and_fetch
    return build_and_fetch(*args, market_data_provider=market_data_provider,
        fetch_symbol_request_fn=provider_by_name(market_data_provider).fetch_symbol_request, **kwargs)


def open_quote_gateway(*, market_data_provider="opend", **kwargs):
    return provider_by_name(market_data_provider).open_quote_gateway(**kwargs)


def resolve_quote_route(config, **kwargs):
    return resolve_market_data_provider(config).resolve_quote_route(config, **kwargs)


def get_stock_spot(*args, market_data_provider="opend", **kwargs):
    return provider_by_name(market_data_provider).get_stock_spot(*args, **kwargs)


def get_trading_days_with_receipt(gateway, *, market_data_provider="opend", **kwargs):
    return provider_by_name(market_data_provider).get_trading_days_with_receipt(gateway, **kwargs)


def get_market_state(gateway, codes, *, market_data_provider="opend"):
    return provider_by_name(market_data_provider).get_market_state(gateway, codes)


def get_underlier_observation(*args, market_data_provider="opend", **kwargs):
    return provider_by_name(market_data_provider).get_underlier_observation(*args, **kwargs)


def discover_option_expirations(*args, market_data_provider="opend", **kwargs):
    return provider_by_name(market_data_provider).discover_option_expirations(*args, **kwargs)


def get_underlier_observations(*args, market_data_provider="opend", **kwargs):
    return provider_by_name(market_data_provider).get_underlier_observations(*args, **kwargs)


def fetch_option_chains(*, market_data_provider="opend", **kwargs):
    return provider_by_name(market_data_provider).fetch_option_chains(**kwargs)


def prefetch_market_earnings_calendars(*, market_data_provider="opend", **kwargs):
    return provider_by_name(market_data_provider).prefetch_earnings_calendars(**kwargs)
