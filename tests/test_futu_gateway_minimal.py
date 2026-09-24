"""Minimal tests for futu_gateway adapter (no futu/OpenD dependency)."""

from __future__ import annotations

from pathlib import Path

import pytest


def _build_gateway(backend_cls, client_cls, **overrides):
    from src.infrastructure.futu_gateway import build_futu_gateway

    return build_futu_gateway(backend_cls=backend_cls, client_cls=client_cls, **overrides)


class _BrokerClient:
    def __init__(self, backend, **_kwargs):
        self.backend = backend

    @staticmethod
    def _unwrap(value):
        return value[1]

    @staticmethod
    def _rows(value):
        return list(value)


def test_build_gateway_with_mock_backend_and_snapshot_call() -> None:

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

        def get_snapshot(self, **kwargs):
            return {"backend_host": self.backend.host, "codes": kwargs.get("code_list") or []}

        def get_stock_basicinfo(self, **kwargs):
            return kwargs

    gw = _build_gateway(FakeBackend, FakeClient, host="127.0.0.9", port=11119, is_option_chain_cache_enabled=True)
    data = gw.get_snapshot(["US.NVDA", "US.TSLA"])

    assert gw.host == "127.0.0.9"
    assert gw.port == 11119
    assert data["backend_host"] == "127.0.0.9"
    assert data["codes"] == ["US.NVDA", "US.TSLA"]
    basic = gw.get_stock_basicinfo(market="US", codes=["US.NVDA"])
    assert basic == {
        "market": "US",
        "stock_type": "STOCK",
        "code_list": ["US.NVDA"],
    }


def test_futu_api_client_stock_basicinfo_unwraps_quote_result() -> None:
    from src.infrastructure.futu_gateway import _FutuAPIClient

    class FakeQuote:
        def __init__(self) -> None:
            self.calls = []

        def get_stock_basicinfo(self, **kwargs):
            self.calls.append(dict(kwargs))
            return 0, [{"code": "US.NVDA", "name": "NVIDIA"}]

    class FakeBackend:
        def __init__(self) -> None:
            self.quote = FakeQuote()

        def _ensure_quote_client(self):
            return self.quote

    backend = FakeBackend()
    client = _FutuAPIClient(backend, is_option_chain_cache_enabled=False)

    rows = client.get_stock_basicinfo(market="US", stock_type="STOCK", code_list=["US.NVDA"])

    assert rows == [{"code": "US.NVDA", "name": "NVIDIA"}]
    assert backend.quote.calls == [
        {
            "market": "US",
            "stock_type": "STOCK",
            "code_list": ["US.NVDA"],
        }
    ]


@pytest.mark.parametrize(
    ("code", "create_time", "expected_auto"),
    [
        ("HK.TCH260730P440000", "2026-07-30 19:25:32", True),
        ("HK.TCH260730P440000", "2026-07-31 23:59:59", True),
        ("HK.TCH260730P440000", "2026-07-29 23:59:59", False),
        ("HK.TCH260730P440000", "2026-08-01 00:00:00", False),
        ("US.PDD260828C100000", "2026-08-29 00:35:08", True),
        ("US.PDD260828C100000", "2026-08-29 00:37:49", True),
        ("US.PDD260821C105000", "2026-08-22 01:06:53", True),
        ("US.PDD261231C100000", "2027-01-01 00:35:08", True),
        ("US.PDD280229C100000", "2028-03-01 00:35:08", True),
        ("US.PDD260828C100000", "2026-08-30 00:00:00", False),
        ("US.PDD260828C100000", "2026-02-30 00:35:08", False),
        ("US.PDD260230C100000", "2026-02-30 00:35:08", False),
        ("US.PDD260828C100000", "bad-date", False),
        ("US.PDD260828C100000", "20260829", False),
        ("US.PDD260828C100000", None, False),
    ],
)
def test_futu_api_client_annotates_only_exact_native_expiry_order_shape(
    code: str,
    create_time: str | None,
    expected_auto: bool,
) -> None:
    from src.infrastructure.futu_gateway import _FutuAPIClient

    base_row = {
        "order_id": "synthetic-expiry-order",
        "code": code,
        "trd_side": "SELL" if code.startswith("US.") else "BUY_BACK",
        "order_type": "NORMAL",
        "order_status": "FILLED_ALL",
        "qty": 2.0,
        "price": 0.0,
        "dealt_qty": 2.0,
        "dealt_avg_price": 0.0,
        "last_err_msg": "",
        "remark": "",
        "create_time": create_time,
    }

    class FakeTrade:
        def history_order_list_query(self, **_kwargs):
            positive_row = dict(base_row, order_id="manual", price=0.01)
            wrong_day_row = dict(base_row, order_id="wrong-day", create_time="2020-01-01 16:00:00")
            return 0, [
                base_row, positive_row, wrong_day_row,
                dict(base_row, order_origin="client"),
                dict(base_row, is_broker_auto=False),
                dict(base_row, dealt_qty=1),
                dict(base_row, dealt_avg_price=0.01),
                dict(base_row, remark="manual"),
            ], None

    class FakeBackend:
        def __init__(self) -> None:
            self.trade = FakeTrade()

        def _ensure_trade_client(self):
            return self.trade

    client = _FutuAPIClient(FakeBackend(), is_option_chain_cache_enabled=False)

    receipt = client.get_history_orders(acc_id="1001", trd_env="REAL")

    assert receipt["rows"][0].get("order_origin") == (
        "broker_auto" if expected_auto else None
    )
    assert receipt["rows"][0].get("order_origin_evidence") == (
        "futu_zero_price_expiry_shape.v2" if expected_auto else None
    )
    assert all(receipt["rows"][0][key] == value for key, value in base_row.items())
    assert "order_origin" not in receipt["rows"][1]
    assert "order_origin" not in receipt["rows"][2]
    assert receipt["rows"][3]["order_origin"] == "client"
    assert all("order_origin" not in row for row in receipt["rows"][4:])
    assert "order_origin" not in base_row


def test_get_trading_days_normalizes_market_label() -> None:

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

        def get_trading_days(self, **kwargs):
            return {"market": kwargs.get("market"), "start": kwargs.get("start")}

        def get_trading_days_with_receipt(self, **kwargs):
            return {"market": kwargs.get("market"), "coverage_complete": True}

    gw = _build_gateway(FakeBackend, FakeClient, host="127.0.0.9", port=11119, is_option_chain_cache_enabled=True)

    data = gw.get_trading_days(market="us", start="2026-08-01")
    assert data["market"] == "US"
    receipt = gw.get_trading_days_with_receipt(market="HK", start="2026-08-01")
    assert receipt["market"] == "HK"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ((0, [{"time": "2026-09-24", "trade_date_type": "WHOLE"}]), None),
        ((0, [{"time": "2026-09-24", "trade_date_type": "WHOLE"}, "bad"]), ValueError),
        ((None, [{"time": "2026-09-24", "trade_date_type": "WHOLE"}]), RuntimeError),
    ],
)
def test_trading_calendar_receipt_requires_complete_success_response(
    response, error,
) -> None:
    from src.infrastructure.futu_gateway import _FutuAPIClient

    class FakeQuote:
        def request_trading_days(self, **_kwargs):
            return response

    class FakeBackend:
        def _ensure_clients(self):
            return FakeQuote(), None

    client = _FutuAPIClient(FakeBackend(), is_option_chain_cache_enabled=False)
    if error:
        with pytest.raises(error):
            client.get_trading_days_with_receipt(
                market="US", start="2026-09-24", end="2026-09-25"
            )
    else:
        receipt = client.get_trading_days_with_receipt(
            market="US", start="2026-09-24", end="2026-09-25"
        )
        assert receipt == {
            "retcode": 0, "rows": response[1], "coverage_complete": True,
            "pagination_complete": True, "page_count": 1,
        }


def test_get_trading_days_rejects_unknown_market() -> None:
    import pytest

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

        def get_trading_days(self, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("client must not be called for unknown market")

    gw = _build_gateway(FakeBackend, FakeClient, host="127.0.0.9", port=11119, is_option_chain_cache_enabled=True)

    with pytest.raises(Exception, match="unsupported trade date market"):
        gw.get_trading_days(market="MOON", start="2026-08-01")


def test_gateway_error_mapping_need_2fa() -> None:

    from src.infrastructure.futu_gateway import FutuGatewayNeed2FAError

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

        def get_snapshot(self, **kwargs):
            raise RuntimeError("phone verification required")

    gw = _build_gateway(FakeBackend, FakeClient)
    try:
        _ = gw.get_snapshot(["US.AAPL"])
    except FutuGatewayNeed2FAError:
        pass
    else:
        raise AssertionError("expected FutuGatewayNeed2FAError")

def test_build_ready_gateway_ensures_quote_ready() -> None:

    from src.infrastructure.futu_gateway import build_ready_futu_gateway

    class FakeQuote:
        def __init__(self) -> None:
            self.ready_calls = 0

        def get_global_state(self):
            self.ready_calls += 1
            return 0, {"program_status_type": "READY", "qot_logined": True}

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port
            self.quote = FakeQuote()

        def _ensure_clients(self):
            return self.quote, None

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

    gw = build_ready_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)
    assert gw.host == "127.0.0.1"
    assert gw.port == 11111
    assert gw.backend.quote.ready_calls == 1


def test_retry_futu_gateway_call_retries_transient_once(monkeypatch) -> None:

    from src.infrastructure.futu_gateway import FutuGatewayTransientError, retry_futu_gateway_call

    calls = {"count": 0}
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr("random.uniform", lambda _a, _b: 0.0)

    def _fn():
        calls["count"] += 1
        if calls["count"] == 1:
            raise FutuGatewayTransientError("temporary")
        return "ok"

    out = retry_futu_gateway_call("test_call", _fn, retry_max_attempts=2)

    assert out == "ok"
    assert calls["count"] == 2


def test_gateway_request_history_kline_returns_page_key() -> None:

    class FakeQuote:
        def __init__(self) -> None:
            self.kwargs = None

        def request_history_kline(self, **kwargs):
            self.kwargs = dict(kwargs)
            return 0, [{"code": "US.NVDA", "close": 900}], "next-page"

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port
            self.quote = FakeQuote()

        def _ensure_clients(self):
            return self.quote, None

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

    gw = _build_gateway(FakeBackend, FakeClient)
    out = gw.request_history_kline(
        code="US.NVDA",
        start="2026-05-01",
        end="2026-05-03",
        ktype="K_DAY",
        autype="NONE",
        fields=[],
        page_req_key=None,
    )

    assert out == {"data": [{"code": "US.NVDA", "close": 900}], "page_req_key": "next-page"}
    kwargs = dict(gw.backend.quote.kwargs)
    assert kwargs.pop("autype") in {"NONE", "None"}
    assert kwargs == {
        "code": "US.NVDA",
        "start": "2026-05-01",
        "end": "2026-05-03",
        "ktype": "K_DAY",
    }


def test_gateway_request_history_kline_maps_canonical_fields_to_sdk(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    fake_futu = SimpleNamespace(
        KLType=SimpleNamespace(K_DAY="k-day"),
        AuType=SimpleNamespace(QFQ="qfq"),
        KL_FIELD=SimpleNamespace(DATE_TIME="date-time", CLOSE="close", TRADE_VOL="trade-vol"),
    )
    monkeypatch.setitem(sys.modules, "futu", fake_futu)

    class FakeQuote:
        def __init__(self) -> None:
            self.kwargs = None

        def request_history_kline(self, **kwargs):
            self.kwargs = dict(kwargs)
            return 0, [], None

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port
            self.quote = FakeQuote()

        def _ensure_clients(self):
            return self.quote, None

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.is_option_chain_cache_enabled = is_option_chain_cache_enabled

    gateway = _build_gateway(FakeBackend, FakeClient)

    gateway.request_history_kline(
        code="US.NVDA",
        start="2026-07-01",
        end="2026-08-06",
        ktype="K_DAY",
        autype="QFQ",
        fields=["time_key", "close", "volume"],
    )

    assert gateway.backend.quote.kwargs["fields"] == ["date-time", "close", "trade-vol"]


def test_gateway_market_state_delegates_to_quote_client() -> None:
    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.calls = []

        def get_market_state(self, **kwargs):
            self.calls.append(dict(kwargs))
            return [{"code": "US.NVDA", "market_state": "MORNING"}]

    gateway = _build_gateway(FakeBackend, FakeClient)

    rows = gateway.get_market_state(["US.NVDA"])

    assert rows == [{"code": "US.NVDA", "market_state": "MORNING"}]
    assert gateway.client.calls == [{"code_list": ["US.NVDA"]}]


def test_gateway_earnings_calendar_delegates_exact_market_window() -> None:
    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend
            self.calls = []

        def get_earnings_calendar(self, **kwargs):
            self.calls.append(dict(kwargs))
            return [
                {
                    "security": "US.NVDA",
                    "earnings_date": "2026-08-19",
                    "earnings_timestamp": 1787108400.0,
                    "pub_type": "AFTER",
                }
            ]

    gw = _build_gateway(FakeBackend, FakeClient)

    rows = gw.get_earnings_calendar(market="US", begin_date="2026-08-17", end_date="2026-08-21")

    assert rows == [
        {
            "security": "US.NVDA",
            "earnings_date": "2026-08-19",
            "earnings_timestamp": 1787108400.0,
            "pub_type": "AFTER",
        }
    ]
    assert gw.client.calls == [
        {
            "market": "US",
            "begin_date": "2026-08-17",
            "end_date": "2026-08-21",
        }
    ]


def test_futu_api_client_earnings_calendar_unwraps_empty_result() -> None:
    from src.infrastructure.futu_gateway import _FutuAPIClient

    class FakeQuote:
        def __init__(self) -> None:
            self.calls = []

        def get_earnings_calendar(self, **kwargs):
            self.calls.append(dict(kwargs))
            return 0, []

    class FakeBackend:
        def __init__(self) -> None:
            self.quote = FakeQuote()

        def _ensure_quote_client(self):
            return self.quote

    backend = FakeBackend()
    client = _FutuAPIClient(backend, is_option_chain_cache_enabled=False)

    assert client.get_earnings_calendar(market="HK", begin_date="2026-08-06", end_date="2026-08-06") == []
    assert backend.quote.calls == [
        {
            "market": "HK",
            "begin_date": "2026-08-06",
            "end_date": "2026-08-06",
        }
    ]


def test_futu_api_client_earnings_calendar_fails_with_stable_capability_reason() -> None:
    from src.infrastructure.futu_gateway import (
        FutuGatewayCapabilityUnavailableError,
        _FutuAPIClient,
    )

    class FakeBackend:
        def _ensure_quote_client(self):
            return object()

    client = _FutuAPIClient(FakeBackend(), is_option_chain_cache_enabled=False)

    with pytest.raises(FutuGatewayCapabilityUnavailableError) as _caught:
        client.get_earnings_calendar(market="US", begin_date="2026-08-06", end_date="2026-08-06")
    exc = _caught.value
    assert exc.code == "CAPABILITY_UNAVAILABLE"
    assert exc.reason_code == "opend_earnings_calendar_unsupported"
    assert exc.capability == "get_earnings_calendar"


def test_inspect_futu_sdk_earnings_calendar_capability_requires_version_and_method(tmp_path: Path) -> None:
    from src.infrastructure.futu_gateway import (
        FUTU_EARNINGS_CALENDAR_MIN_VERSION,
        inspect_futu_sdk_earnings_calendar_capability,
    )

    package_root = tmp_path / "futu"
    quote_dir = package_root / "quote"
    quote_dir.mkdir(parents=True)
    source = quote_dir / "open_quote_context.py"
    source.write_text(
        "class OpenQuoteContext:\n"
        "    def get_earnings_calendar(self, market, begin_date=None, end_date=None):\n"
        "        return market, begin_date, end_date\n",
        encoding="utf-8",
    )

    supported = inspect_futu_sdk_earnings_calendar_capability(
        package_root=package_root,
        installed_version=FUTU_EARNINGS_CALENDAR_MIN_VERSION,
    )
    old = inspect_futu_sdk_earnings_calendar_capability(package_root=package_root, installed_version="10.8.6808")
    source.write_text("class OpenQuoteContext:\n    pass\n", encoding="utf-8")
    missing_method = inspect_futu_sdk_earnings_calendar_capability(
        package_root=package_root,
        installed_version=FUTU_EARNINGS_CALENDAR_MIN_VERSION,
    )

    assert supported["supported"] is True
    assert supported["reason_code"] is None
    assert old["supported"] is False
    assert old["reason_code"] == "futu_api_version_too_old"
    assert missing_method["supported"] is False
    assert missing_method["reason_code"] == "opend_earnings_calendar_unsupported"


def test_broker_ready_builder_never_constructs_quote_context() -> None:
    from src.infrastructure.futu_gateway import build_ready_futu_broker_gateway

    class Trade:
        def get_global_state(self):
            return 0, {"program_status_type": "READY", "trd_logined": True}

        def get_acc_list(self):
            return 0, [{"acc_id": "1001", "trd_env": "REAL"}]

        def close(self):
            pass

    class Backend:
        def __init__(self, **_kwargs):
            self._quote_client = None
            self._trade_client = None

        def _ensure_quote_client(self):
            raise AssertionError("quote client must not be constructed")

        def _ensure_trade_client(self):
            if self._trade_client is None:
                self._trade_client = Trade()
            return self._trade_client

    Client = _BrokerClient

    gateway = build_ready_futu_broker_gateway(
        host="broker",
        port=11112,
        expected_account_ids=["1001"],
        trd_env="REAL",
        backend_cls=Backend,
        client_cls=Client,
    )

    assert gateway.backend._quote_client is None


def test_default_broker_adapter_converts_canonical_string_account_id_to_sdk_integer(
) -> None:
    from src.infrastructure.futu_gateway import build_futu_gateway

    account_id = "999000000000000001"

    class Trade:
        def __init__(self) -> None:
            self.calls = []

        def _record(self, method, kwargs, *, paginated=False):
            self.calls.append((method, dict(kwargs)))
            return (0, [], None) if paginated else (0, [])

        def position_list_query(self, **kwargs):
            return self._record("positions", kwargs)

        def accinfo_query(self, **kwargs):
            return self._record("balance", kwargs)

        def acctradinginfo_query(self, **kwargs):
            return self._record("funds", kwargs)

        def order_list_query(self, **kwargs):
            return self._record("orders", kwargs)

        def deal_list_query(self, **kwargs):
            return self._record("deals", kwargs)

        def history_order_list_query(self, **kwargs):
            return self._record("history_orders", kwargs, paginated=True)

        def history_deal_list_query(self, **kwargs):
            return self._record("history_deals", kwargs, paginated=True)

        def order_fee_query(self, **kwargs):
            return self._record("order_fees", kwargs)

    class Backend:
        def __init__(self, **_kwargs):
            self.trade = Trade()

        def _ensure_trade_client(self):
            return self.trade

    gateway = build_futu_gateway(backend_cls=Backend)
    common = {"acc_id": account_id, "trd_env": "REAL"}

    gateway.get_positions(**common)
    gateway.get_account_balance(**common)
    gateway.get_funds(**common)
    gateway.get_order_list(**common)
    gateway.get_deal_list(**common)
    gateway.get_history_orders(**common)
    gateway.get_history_deals(**common)
    gateway.get_order_fees(order_id_list=["order-1"], **common)
    gateway.get_positions_with_receipt(**common)

    assert [name for name, _kwargs in gateway.backend.trade.calls] == [
        "positions",
        "balance",
        "funds",
        "orders",
        "deals",
        "history_orders",
        "history_deals",
        "order_fees",
        "positions",
    ]
    assert all(
        kwargs["acc_id"] == int(account_id)
        and isinstance(kwargs["acc_id"], int)
        for _name, kwargs in gateway.backend.trade.calls
    )
    assert common["acc_id"] == account_id


def test_broker_readiness_requires_every_identity_in_requested_environment() -> None:
    from src.infrastructure.futu_gateway import FutuGatewayError, build_ready_futu_broker_gateway

    class Trade:
        def get_global_state(self):
            return 0, {"program_status_type": "READY", "trd_logined": True}

        def get_acc_list(self):
            return 0, [
                {"acc_id": "1001", "trd_env": "REAL"},
                {"acc_id": "1002", "trd_env": "SIMULATE"},
            ]

        def close(self):
            pass

    class Backend:
        def __init__(self, **_kwargs):
            self._quote_client = None
            self._trade_client = Trade()

        def _ensure_trade_client(self):
            return self._trade_client

    Client = _BrokerClient

    with pytest.raises(FutuGatewayError) as _caught:
        build_ready_futu_broker_gateway(
            expected_account_ids=["1001", "1002"],
            trd_env="REAL",
            backend_cls=Backend,
            client_cls=Client,
        )
    exc = _caught.value
    assert "1002" not in str(exc)
    assert "****" in str(exc)


def test_account_metadata_reads_only_exact_simulated_account_list_match() -> None:
    from src.infrastructure.futu_gateway import build_futu_gateway

    class Trade:
        calls = 0

        def get_acc_list(self):
            self.calls += 1
            return 0, [
                {
                    "acc_id": "90000001",
                    "trd_env": "SIMULATE",
                    "sim_acc_type": "OPTION",
                    "trdmarket_auth": ["US"],
                },
                {
                    "acc_id": "90000002",
                    "trd_env": "SIMULATE",
                    "sim_acc_type": "OPTION",
                    "trdmarket_auth": ["HK"],
                },
                {"acc_id": "90000001", "trd_env": "REAL"},
            ]

    class Backend:
        def __init__(self, **_kwargs):
            self._trade_client = Trade()

        def _ensure_trade_client(self):
            return self._trade_client

    Client = _BrokerClient

    gateway = build_futu_gateway(backend_cls=Backend, client_cls=Client)
    metadata = gateway.get_account_metadata(expected_account_id="90000001", trd_env="SIMULATE", expected_market="US")

    assert metadata == {
        "matched": True,
        "trd_env": "SIMULATE",
        "sim_acc_type": "OPTION",
        "trdmarket_auth": ["US"],
        "account_id_tail": "0001",
        "same_type_count": 1,
    }
    assert gateway.backend._trade_client.calls == 1


def test_broker_readiness_rejects_missing_explicit_global_state_facts() -> None:
    from src.infrastructure.futu_gateway import FutuGatewayError, build_ready_futu_broker_gateway

    class Trade:
        def get_global_state(self):
            return 0, {}

        def get_acc_list(self):
            return 0, [{"acc_id": "1001", "trd_env": "REAL"}]

        def close(self):
            pass

    class Backend:
        def __init__(self, **_kwargs):
            self._quote_client = None
            self._trade_client = Trade()

        def _ensure_trade_client(self):
            return self._trade_client

    Client = _BrokerClient

    with pytest.raises(FutuGatewayError) as _caught:
        build_ready_futu_broker_gateway(
            expected_account_ids=["1001"],
            trd_env="REAL",
            backend_cls=Backend,
            client_cls=Client,
        )
    exc = _caught.value
    assert "not READY" in str(exc)


def test_default_backend_quote_readiness_does_not_construct_trade_context(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from src.infrastructure import futu_gateway
    from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway

    monkeypatch.setattr(futu_gateway, "port_open", lambda host, port: True)

    calls = {"quote": 0, "trade": 0}

    class Quote:
        def __init__(self, **_kwargs):
            calls["quote"] += 1

        def get_global_state(self):
            return 0, {"program_status_type": "READY", "qot_logined": True}

        def close(self):
            pass

    class Trade:
        def __init__(self, **_kwargs):
            calls["trade"] += 1

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            RET_OK=0,
            OpenQuoteContext=Quote,
            OpenSecTradeContext=Trade,
        ),
    )

    gateway = build_ready_futu_quote_gateway()
    gateway.close()

    assert calls == {"quote": 1, "trade": 0}


def test_unreachable_backend_fails_fast_without_sdk_context(monkeypatch) -> None:
    """Port-closed OpenD must raise UNREACHABLE quickly, never enter SDK reconnect loop."""

    import sys
    import time
    from types import SimpleNamespace

    from src.infrastructure import futu_gateway as mod

    monkeypatch.setattr(mod, "port_open", lambda host, port: False)
    constructed: list[tuple[str, int]] = []

    class FakeQuote:
        def __init__(self, host, port, **kwargs):
            constructed.append((host, port))

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(RET_OK=0, OpenQuoteContext=FakeQuote, OpenSecTradeContext=FakeQuote),
    )

    t0 = time.monotonic()
    with pytest.raises(mod.FutuGatewayUnreachableError) as exc_info:
        mod.build_ready_futu_quote_gateway(host="127.0.0.9", port=11119)
    elapsed = time.monotonic() - t0
    assert exc_info.value.code == "UNREACHABLE"
    assert elapsed < 1.0
    assert constructed == []


def test_unreachable_trade_client_fails_fast(monkeypatch) -> None:
    import sys
    import time
    from types import SimpleNamespace

    from src.infrastructure import futu_gateway as mod

    monkeypatch.setattr(mod, "port_open", lambda host, port: False)
    constructed: list[tuple[str, int]] = []

    class FakeTrade:
        def __init__(self, host, port, **kwargs):
            constructed.append((host, port))

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(RET_OK=0, OpenQuoteContext=FakeTrade, OpenSecTradeContext=FakeTrade),
    )

    t0 = time.monotonic()
    with pytest.raises(mod.FutuGatewayUnreachableError):
        mod.build_ready_futu_broker_gateway(host="127.0.0.9", port=11119, expected_account_ids=[], trd_env="REAL")
    assert time.monotonic() - t0 < 1.0
    assert constructed == []


def test_port_open_preserves_original_sdk_path(monkeypatch) -> None:
    """port_open=True must keep constructing the SDK context (original semantics)."""

    import sys
    from types import SimpleNamespace

    from src.infrastructure import futu_gateway as mod

    monkeypatch.setattr(mod, "port_open", lambda host, port: True)
    constructed: list[tuple[str, int]] = []

    class FakeQuote:
        def __init__(self, host, port, **kwargs):
            constructed.append((host, port))

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(RET_OK=0, OpenQuoteContext=FakeQuote, OpenSecTradeContext=FakeQuote),
    )

    with pytest.raises(mod.FutuGatewayError):
        mod.build_ready_futu_quote_gateway(host="127.0.0.9", port=11119)
    assert constructed == [("127.0.0.9", 11119)]
