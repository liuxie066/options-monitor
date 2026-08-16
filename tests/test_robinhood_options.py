from __future__ import annotations

from http.client import RemoteDisconnected

import pytest

from src.infrastructure import robinhood_options
from src.infrastructure.robinhood_options import RobinhoodOptionsError, normalize_robinhood_option_chain_payload


def test_normalize_robinhood_option_chain_payload_merges_instruments_and_marketdata() -> None:
    option_instruments = [
        {
            "id": "option-id-1",
            "url": "https://api.robinhood.com/options/instruments/option-id-1/",
            "chain_symbol": "AAPL",
            "expiration_date": "2026-01-16",
            "type": "call",
            "strike_price": "150.0000",
            "state": "active",
            "tradability": "tradable",
        },
        {
            "id": "option-id-2",
            "url": "https://api.robinhood.com/options/instruments/option-id-2/",
            "chain_symbol": "AAPL",
            "expiration_date": "2026-01-16",
            "type": "put",
            "strike_price": "140.0000",
            "state": "active",
            "tradability": "tradable",
        },
    ]
    marketdata_by_instrument = {
        "https://api.robinhood.com/options/instruments/option-id-1/": {
            "instrument": "https://api.robinhood.com/options/instruments/option-id-1/",
            "bid_price": "12.10",
            "ask_price": "12.40",
            "adjusted_mark_price": "12.25",
            "last_trade_price": "12.00",
            "volume": "100",
            "open_interest": "500",
            "implied_volatility": "0.32",
            "delta": "0.7",
            "gamma": "0.01",
            "theta": "-0.03",
            "vega": "0.1",
            "chance_of_profit_long": "0.62",
        },
        "option-id-2": {
            "instrument": "https://api.robinhood.com/options/instruments/option-id-2/",
            "bid_price": "3.20",
            "ask_price": "3.40",
            "mark_price": "3.30",
            "last_trade_price": "3.10",
            "volume": "20",
            "open_interest": "600",
            "implied_volatility": "0.35",
            "delta": "-0.2",
        },
    }

    rows = normalize_robinhood_option_chain_payload(
        option_instruments,
        marketdata_by_instrument=marketdata_by_instrument,
        requested_symbol="aapl",
    )

    assert rows[0] == {
        "provider": "robinhood",
        "data_mode": "robinhood",
        "symbol": "AAPL",
        "underlier_code": "AAPL",
        "contract_symbol": "AAPL260116C00150000",
        "option_code": "AAPL260116C00150000",
        "option_id": "option-id-1",
        "instrument_url": "https://api.robinhood.com/options/instruments/option-id-1/",
        "option_type": "call",
        "expiration": "2026-01-16",
        "expiration_ymd": "2026-01-16",
        "strike": 150.0,
        "strike_price": 150.0,
        "bid": 12.1,
        "bid_price": 12.1,
        "ask": 12.4,
        "ask_price": 12.4,
        "mid": 12.25,
        "last": 12.0,
        "last_price": 12.0,
        "volume": 100,
        "open_interest": 500,
        "iv": 0.32,
        "delta": 0.7,
        "gamma": 0.01,
        "theta": -0.03,
        "vega": 0.1,
        "chance_of_profit_long": 0.62,
        "chance_of_profit_short": None,
        "bid_ask_spread": 0.3,
        "state": "active",
        "tradability": "tradable",
        "updated_at": None,
        "raw_marketdata": marketdata_by_instrument["https://api.robinhood.com/options/instruments/option-id-1/"],
    }
    assert rows[1]["contract_symbol"] == "AAPL260116P00140000"
    assert rows[1]["option_type"] == "put"
    assert rows[1]["delta"] == -0.2


def test_robinhood_request_json_wraps_remote_disconnect(monkeypatch) -> None:
    def _disconnect(*_args, **_kwargs):
        raise RemoteDisconnected("closed")

    monkeypatch.setattr(robinhood_options, "urlopen", _disconnect)

    with pytest.raises(RobinhoodOptionsError, match="closed the connection"):
        robinhood_options._request_json("https://api.robinhood.com/test/", params={}, token="token", timeout=1)
