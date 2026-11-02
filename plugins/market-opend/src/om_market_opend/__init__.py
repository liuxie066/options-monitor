"""OpenD market capability; account access belongs to broker plugins."""

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

    def get_quote_rights(self, gateway, *, market):
        return gateway.get_quote_rights(market=market)

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


__all__ = ["OpenDMarketDataProvider"]
