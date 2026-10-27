from __future__ import annotations


def _config(market: str, endpoints: list[tuple[str, int]]) -> dict:
    return {
        "_generated": {"market": market},
        "symbols": [
            {"symbol": f"S{index}", "fetch": {"source": "futu", "host": host, "port": port}}
            for index, (host, port) in enumerate(endpoints)
        ],
    }


def test_quote_route_uses_effective_futu_fetch_bindings_only() -> None:
    from src.application.futu_quote_routing import resolve_futu_quote_route

    cfg = _config("us", [("QUOTE.local", 11111), ("quote.LOCAL", 11111)])
    cfg["account_settings"] = {"lx": {"futu": {"host": "broker", "port": 22222}}}
    route = resolve_futu_quote_route(cfg, config_key="us")

    assert route.ok is True
    assert (route.host, route.port) == ("quote.local", 11111)
    assert {member.symbol for member in route.members} == {"S0", "S1"}


def test_quote_route_reports_missing_and_conflict_without_fallback() -> None:
    from src.application.futu_quote_routing import resolve_futu_quote_route

    missing = resolve_futu_quote_route({"symbols": []}, config_key="us")
    conflict = resolve_futu_quote_route(
        _config("us", [("one", 11111), ("two", 11112)]), config_key="us"
    )

    assert missing.status == "missing"
    assert missing.host is None
    assert conflict.status == "conflict"
    assert conflict.host is None


def test_shared_quote_route_requires_all_markets_to_converge() -> None:
    from src.application.futu_quote_routing import resolve_shared_futu_quote_route

    shared = resolve_shared_futu_quote_route(
        [("us", _config("us", [("q", 11111)])), ("hk", _config("hk", [("q", 11111)]))]
    )
    split = resolve_shared_futu_quote_route(
        [("us", _config("us", [("q", 11111)])), ("hk", _config("hk", [("q", 11112)]))]
    )

    assert shared.ok is True
    assert (shared.host, shared.port) == ("q", 11111)
    assert split.status == "conflict"


def test_known_symbol_uses_its_binding_despite_other_endpoint():
    from src.application.futu_quote_routing import resolve_futu_quote_route
    config = _config('us', [('one', 11111), ('two', 11112)])
    assert resolve_futu_quote_route(config, symbol='US.S0').host == 'one'
    assert resolve_futu_quote_route(config, symbol='NVDA').status == 'conflict'


def test_known_non_futu_symbol_does_not_inherit_other_binding():
    from src.application.futu_quote_routing import resolve_futu_quote_route
    config = _config('us', [('one', 11111)])
    config['symbols'].append({'symbol': 'NVDA', 'fetch': {'source': 'file'}})
    assert resolve_futu_quote_route(config, symbol='NVDA').status == 'missing'


def test_symbol_binding_aliases_and_duplicate_rejected():
    from src.application.futu_quote_routing import resolve_futu_quote_route
    config = {'_generated': {'market': 'hk'}, 'symbols': [
        {'symbol': '0700.HK', 'fetch': {'source': 'futu', 'host': 'quote', 'port': 11111}}]}
    assert resolve_futu_quote_route(config, symbol='00700').host == 'quote'
    config['symbols'].append({'symbol': 'HK.00700', 'fetch': {'source': 'futu'}})
    assert resolve_futu_quote_route(config, symbol='00700').status == 'conflict'
