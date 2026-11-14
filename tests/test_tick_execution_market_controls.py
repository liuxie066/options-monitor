"""Budget compatibility for the existing spot owner and OpenD plugin forwarding."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import socket
import time

import pandas as pd
import pytest

from src.application import opend_market_snapshot_fetching as snapshots
from src.application.opend_fetch_config import OpenDEndpointRateLimit


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network is forbidden in market control regressions")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _source_provider():
    path = Path(__file__).resolve().parents[1] / "plugins/market-opend/src/om_market_opend/__init__.py"
    spec = importlib.util.spec_from_file_location("_tick_market_opend_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OpenDMarketDataProvider()


@pytest.mark.parametrize("controls", [{}, {"deadline": 123.0}, {"cancelled": lambda: False},
                                      {"deadline": 123.0, "cancelled": lambda: False}])
def test_plugin_state_and_rights_forward_optional_controls(controls):
    calls = []

    class Gateway:
        def get_market_state(self, codes, **kwargs):
            calls.append(("state", codes, kwargs))
            return "state-result"

        def get_quote_rights(self, *, market, **kwargs):
            calls.append(("rights", market, kwargs))
            return "rights-result"

    provider = _source_provider()
    assert provider.get_market_state(Gateway(), ["US.NVDA"], **controls) == "state-result"
    assert provider.get_quote_rights(Gateway(), market="US", **controls) == "rights-result"
    expected = {"deadline": controls.get("deadline"), "cancelled": controls.get("cancelled")} if controls else {}
    assert calls == [("state", ["US.NVDA"], expected), ("rights", "US", expected)]


def test_plugin_independent_calls_keep_original_gateway_signatures():
    class Gateway:
        def get_market_state(self, codes):
            return codes

        def get_quote_rights(self, *, market):
            return market

    provider = _source_provider()
    assert provider.get_market_state(Gateway(), ["US.NVDA"]) == ["US.NVDA"]
    assert provider.get_quote_rights(Gateway(), market="US") == "US"


@pytest.mark.parametrize("deadline_offset,stop_offset", [(10.0, 20.0), (20.0, 10.0)])
def test_spot_owner_preserves_earlier_deadline_across_endpoints(
    monkeypatch, tmp_path, deadline_offset, stop_offset,
):
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    calls = []
    retries = []
    gateway_calls = []
    cancelled = lambda: False

    def retry(_name, call, **kwargs):
        retries.append(kwargs)
        return call()

    def rate(**kwargs):
        calls.append(kwargs)
        return kwargs["call"]()

    monkeypatch.setattr(snapshots, "retry_futu_gateway_call", retry)

    class Gateway:
        def get_snapshot(self, codes, **kwargs):
            gateway_calls.append(("snapshot", kwargs))
            return pd.DataFrame([{"code": code, "last_price": 100, "update_time": now.isoformat(),
                                  "sec_status": "NORMAL", "suspension": False} for code in codes])

        def get_market_state(self, codes, **kwargs):
            gateway_calls.append(("state", kwargs))
            return pd.DataFrame([{"code": code, "market_state": "MORNING"} for code in codes])

    result = snapshots.get_underlier_observations_opend(
        Gateway(), ["US.NVDA"], market="US", base_dir=tmp_path,
        snapshot_limit=OpenDEndpointRateLimit(window_sec=30, max_calls=60, max_wait_sec=30),
        snapshot_batch_size=1, stop_monotonic=started + stop_offset,
        deadline=started + deadline_offset, cancelled=cancelled,
        monotonic=lambda: started, now_utc=lambda: now, rate_limited_call=rate,
    )
    expected_stop = started + min(deadline_offset, stop_offset)
    assert result["US.NVDA"].status == "ready"
    assert len(calls) == len(retries) == 2
    assert all(item["deadline"] == expected_stop and item["cancelled"] is cancelled for item in calls)
    assert all(item["deadline"] == expected_stop and item["cancelled"] is cancelled for item in retries)
    assert all(item["max_wait_sec"] <= min(deadline_offset, stop_offset) for item in calls)
    assert gateway_calls == [
        ("snapshot", {"deadline": expected_stop, "cancelled": cancelled}),
        ("state", {"deadline": expected_stop, "cancelled": cancelled}),
    ]


@pytest.mark.parametrize("controlled", [False, True])
@pytest.mark.parametrize("hooked", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_snapshot_owner_forwards_controls_through_receipt_and_fallback(tmp_path, controlled, hooked, recover):
    calls = []
    deadline = time.monotonic() + 10
    cancelled = lambda: False
    controls = {"deadline": deadline, "cancelled": cancelled} if controlled else {}

    class Gateway:
        def get_snapshot(self, codes, **kwargs):
            calls.append((list(codes), kwargs))
            if recover and len(calls) == 1:
                raise RuntimeError("controlled batch failure")
            return pd.DataFrame([{"code": code, "last_price": 1.0} for code in codes])

    class HookedGateway(Gateway):
        def get_snapshot_with_request_hook(self, codes, *, before_request, **kwargs):
            before_request()
            return self.get_snapshot(codes, **kwargs)

    result = snapshots.fetch_option_snapshots(
        option_codes=["US.TEST261218P100000"], gateway=HookedGateway() if hooked else Gateway(),
        base_dir=tmp_path, snapshot_limit=OpenDEndpointRateLimit(window_sec=30, max_calls=60, max_wait_sec=30),
        no_retry=True, retry_call=lambda _name, call, **_kwargs: call(),
        rate_limited_call=lambda **kwargs: kwargs["call"](), **controls,
    )
    assert result.complete and not result.missing_codes
    assert result.errors == []
    assert len(calls) == (2 if recover else 1)
    assert all(kwargs == controls for _codes, kwargs in calls)
    assert all(receipt["provider_call_started"] for receipt in result.batch_receipts)
    assert [receipt["status"] for receipt in result.batch_receipts] == (["failed", "received"] if recover else ["received"])
    assert result.fallback_filled == (1 if recover else 0)


@pytest.mark.parametrize("controlled", [False, True])
def test_stock_quote_owner_forwards_controls_to_snapshot(controlled):
    calls = []
    controls = {"deadline": time.monotonic() + 10, "cancelled": lambda: False} if controlled else {}

    class Gateway:
        def get_snapshot(self, codes, **kwargs):
            calls.append(kwargs)
            return pd.DataFrame([{"code": codes[0], "last_price": 100.0}])

    assert snapshots.get_spot_opend_quote(Gateway(), "US.TEST", **controls)["price"] == 100.0
    assert calls == [controls]


def test_spot_owner_cancellation_stops_before_next_endpoint(tmp_path):
    state = {"cancelled": False}
    calls = []

    class Gateway:
        def get_snapshot(self, codes, **kwargs):
            assert callable(kwargs["cancelled"])
            calls.append("snapshot")
            state["cancelled"] = True
            return pd.DataFrame([{"code": codes[0]}])

        def get_market_state(self, _codes):
            calls.append("state")
            raise AssertionError("cancelled request must not start the next endpoint")

    with pytest.raises(TimeoutError, match="cancelled"):
        snapshots.get_underlier_observations_opend(
            Gateway(), ["US.NVDA"], market="US", base_dir=tmp_path,
            snapshot_limit=OpenDEndpointRateLimit(window_sec=30, max_calls=60, max_wait_sec=30),
            snapshot_batch_size=1, cancelled=lambda: state["cancelled"],
            rate_limited_call=lambda **kwargs: kwargs["call"](),
        )
    assert calls == ["snapshot"]
