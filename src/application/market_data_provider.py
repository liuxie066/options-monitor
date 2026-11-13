"""Installed market-data capability boundary; no provider discovery or live I/O on selection.

Implementations return the existing opening quote, underlier, expiration and
earnings evidence contracts unchanged. Provider choice never grants broker
account access or changes freshness, retry, cancellation or resource budgets.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from om_market_opend import OpenDMarketDataProvider


__all__ = [
    "OpenDMarketDataProvider",
    "resolve_market_data_provider",
    "require_market_data_provider",
    "provider_by_name",
    "market_data_source",
    "fetch_symbol_request",
    "fetch_symbol",
    "open_quote_gateway",
    "resolve_quote_route",
    "get_stock_spot",
    "get_trading_days_with_receipt",
    "get_market_state",
    "get_quote_rights",
    "get_underlier_observation",
    "discover_option_expirations",
    "get_underlier_observations",
    "fetch_option_chains",
    "prefetch_market_earnings_calendars",
]


class _OpenDProviderFacade:
    name = "opend"

    def load(self):
        from src.application.plugin_runtime import require_plugin
        return require_plugin("market-opend").OpenDMarketDataProvider()

    def __getattr__(self, name):
        return getattr(self.load(), name)


def __getattr__(name):
    if name == "OpenDMarketDataProvider":
        from src.application.plugin_runtime import require_plugin
        return require_plugin("market-opend").OpenDMarketDataProvider
    raise AttributeError(name)


# Fixed identity table; tests may inject isolated providers here.
_BUILTIN_PROVIDERS = {"opend": _OpenDProviderFacade()}


def resolve_market_data_provider(config: Mapping[str, Any] | None = None):
    section = (config or {}).get("market_data", {})
    if not isinstance(section, Mapping):
        raise ValueError("market_data must be an object")
    name = section.get("provider", "opend")
    if not isinstance(name, str) or name not in _BUILTIN_PROVIDERS:
        raise ValueError(f"unsupported market_data.provider: {name!r}")
    return _BUILTIN_PROVIDERS[name]


def require_market_data_provider(config: Mapping[str, Any] | None = None):
    """Admit a live operation without making historical identity reads import plugins."""
    provider = resolve_market_data_provider(config)
    return provider.load() if isinstance(provider, _OpenDProviderFacade) else provider


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


def get_market_state(gateway, codes, *, market_data_provider="opend", deadline=None, cancelled=None):
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    return provider_by_name(market_data_provider).get_market_state(gateway, codes, **controls)


def get_quote_rights(gateway, *, market, market_data_provider="opend", deadline=None, cancelled=None):
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    return provider_by_name(market_data_provider).get_quote_rights(gateway, market=market, **controls)


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
