"""Corporate action queries through the real gateway, without OpenD access."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from src.application.research.underlier_history import corporate_action_response
from src.infrastructure import futu_gateway


@pytest.fixture
def rehab_gateway(monkeypatch):
    transport = SimpleNamespace(response=(0, []), calls=[], connections=[])

    def get_rehab(*, code):
        transport.calls.append({"code": code})
        return transport.response

    transport.get_rehab = get_rehab

    def quote_context(**kwargs):
        transport.connections.append(kwargs)
        return transport

    def trade_context(**_kwargs):
        pytest.fail("get_rehab must not initialize a trade context")

    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(RET_OK=0))
    monkeypatch.setattr(futu_gateway, "build_futu_quote_context", quote_context)
    monkeypatch.setattr(futu_gateway, "build_futu_trade_context", trade_context)
    gateway = futu_gateway.build_futu_gateway()
    return gateway, transport


@pytest.mark.parametrize("container", ["dataframe", "rows"])
@pytest.mark.parametrize("rows", [[], [{"code": "US.AAPL", "ex_div_date": "2026-10-30", "split_ratio": 2.0}]])
def test_get_rehab_preserves_provider_payload(rehab_gateway, container, rows):
    gateway, transport = rehab_gateway
    payload = pd.DataFrame(rows) if container == "dataframe" else rows
    transport.response = (0, payload)

    assert gateway.get_rehab(code="US.AAPL") is payload
    assert transport.calls == [{"code": "US.AAPL"}]
    assert transport.connections == [{"host": "127.0.0.1", "port": 11111}]
    assert gateway.backend._trade_client is None


def test_get_rehab_rejects_missing_market_plugin_before_transport(rehab_gateway, monkeypatch):
    from src.application import plugin_runtime

    gateway, transport = rehab_gateway
    monkeypatch.setattr(plugin_runtime, "plugin_installed", lambda _plugin_id: False)
    with pytest.raises(plugin_runtime.PluginUnavailableError, match="market-opend"):
        gateway.get_rehab(code="US.AAPL")
    assert transport.connections == []
    assert transport.calls == []
    assert gateway.backend._trade_client is None


@pytest.mark.parametrize("rows", [[], [{"code": "US.AAPL", "ex_div_date": "2026-10-30", "split_ratio": 2.0}]])
def test_corporate_action_response_uses_real_gateway(rehab_gateway, rows):
    gateway, transport = rehab_gateway
    transport.response = (0, pd.DataFrame(rows))
    calls = []

    def call(name, query):
        calls.append(name)
        return query()

    assert corporate_action_response(gateway, code="US.AAPL", call=call) == {"rows": rows}
    assert calls == ["rehab"]
    assert transport.calls == [{"code": "US.AAPL"}]
    assert gateway.backend._trade_client is None


@pytest.mark.parametrize(
    "message,expected_error",
    [
        ("provider query rejected", futu_gateway.FutuGatewayError),
        ("rate limit exceeded", futu_gateway.FutuGatewayRateLimitError),
    ],
)
def test_get_rehab_maps_provider_failure(rehab_gateway, message, expected_error):
    gateway, transport = rehab_gateway
    transport.response = (-1, message)

    with pytest.raises(expected_error, match=f"get_rehab failed: {message}") as raised:
        gateway.get_rehab(code="US.AAPL")

    assert type(raised.value) is expected_error
    assert isinstance(raised.value.raw_error, RuntimeError)
    assert str(raised.value.raw_error) == message
    assert transport.calls == [{"code": "US.AAPL"}]
    assert gateway.backend._trade_client is None
