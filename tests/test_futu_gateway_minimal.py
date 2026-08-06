"""Minimal tests for futu_gateway adapter (no futu/OpenD dependency)."""

from __future__ import annotations

from pathlib import Path


def test_build_gateway_with_mock_backend_and_snapshot_call() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.infrastructure.futu_gateway import build_futu_gateway

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

    gw = build_futu_gateway(
        host="127.0.0.9",
        port=11119,
        is_option_chain_cache_enabled=True,
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )
    data = gw.get_snapshot(["US.NVDA", "US.TSLA"])

    assert gw.host == "127.0.0.9"
    assert gw.port == 11119
    assert data["backend_host"] == "127.0.0.9"
    assert data["codes"] == ["US.NVDA", "US.TSLA"]


def test_gateway_error_mapping_need_2fa() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.infrastructure.futu_gateway import build_futu_gateway, FutuGatewayNeed2FAError

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

    gw = build_futu_gateway(
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )
    try:
        _ = gw.get_snapshot(["US.AAPL"])
    except FutuGatewayNeed2FAError:
        pass
    else:
        raise AssertionError("expected FutuGatewayNeed2FAError")

def test_build_ready_gateway_ensures_quote_ready() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

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

    gw = build_ready_futu_gateway(
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )
    assert gw.host == "127.0.0.1"
    assert gw.port == 11111
    assert gw.backend.quote.ready_calls == 1


def test_retry_futu_gateway_call_retries_transient_once(monkeypatch) -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

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
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.infrastructure.futu_gateway import build_futu_gateway

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

    gw = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)
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


def test_gateway_event_source_methods_delegate_to_quote_client() -> None:
    import sys

    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from src.infrastructure.futu_gateway import build_futu_gateway

    class FakeQuote:
        def __init__(self) -> None:
            self.calls = []

        def get_financials_earnings_price_history(self, **kwargs):
            self.calls.append(("earnings", kwargs))
            return 0, [{"pub_trading_day_str": "2026-06-01"}]

        def get_corporate_actions_dividends(self, **kwargs):
            self.calls.append(("dividends", kwargs))
            return 0, {"dividend_list": [{"ex_date": "2026-06-05"}]}

        def get_corporate_actions_stock_splits(self, **kwargs):
            self.calls.append(("splits", kwargs))
            return 0, {"next_key": "-1", "split_list": [{"ex_date_str": "2026-06-10"}]}

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

        def get_financials_earnings_price_history(self, **kwargs):
            return self.backend.quote.get_financials_earnings_price_history(**kwargs)[1]

        def get_corporate_actions_dividends(self, **kwargs):
            return self.backend.quote.get_corporate_actions_dividends(**kwargs)[1]

        def get_corporate_actions_stock_splits(self, **kwargs):
            return self.backend.quote.get_corporate_actions_stock_splits(**kwargs)[1]

    gw = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)

    assert gw.get_financials_earnings_price_history("US.NVDA") == [{"pub_trading_day_str": "2026-06-01"}]
    assert gw.get_corporate_actions_dividends("US.NVDA") == {"dividend_list": [{"ex_date": "2026-06-05"}]}
    assert gw.get_corporate_actions_stock_splits("US.NVDA", next_key=None, num=50) == {
        "next_key": "-1",
        "split_list": [{"ex_date_str": "2026-06-10"}],
    }


def test_gateway_earnings_calendar_delegates_exact_market_window() -> None:
    from src.infrastructure.futu_gateway import build_futu_gateway

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

    gw = build_futu_gateway(
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )

    rows = gw.get_earnings_calendar(
        market="US",
        begin_date="2026-08-17",
        end_date="2026-08-21",
    )

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

    assert client.get_earnings_calendar(
        market="HK",
        begin_date="2026-08-06",
        end_date="2026-08-06",
    ) == []
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

    try:
        client.get_earnings_calendar(
            market="US",
            begin_date="2026-08-06",
            end_date="2026-08-06",
        )
    except FutuGatewayCapabilityUnavailableError as exc:
        assert exc.code == "CAPABILITY_UNAVAILABLE"
        assert exc.reason_code == "opend_earnings_calendar_unsupported"
        assert exc.capability == "get_earnings_calendar"
    else:
        raise AssertionError("expected FutuGatewayCapabilityUnavailableError")


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
    old = inspect_futu_sdk_earnings_calendar_capability(
        package_root=package_root,
        installed_version="10.8.6808",
    )
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

    class Client:
        def __init__(self, backend, **_kwargs):
            self.backend = backend

        @staticmethod
        def _unwrap(value):
            return value[1]

        @staticmethod
        def _rows(value):
            return list(value)

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

    account_id = "281756479859383816"

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
    gateway.get_positions_with_receipt(**common)

    assert [name for name, _kwargs in gateway.backend.trade.calls] == [
        "positions",
        "balance",
        "funds",
        "orders",
        "deals",
        "history_orders",
        "history_deals",
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

    class Client:
        def __init__(self, backend, **_kwargs):
            self.backend = backend

        @staticmethod
        def _unwrap(value):
            return value[1]

        @staticmethod
        def _rows(value):
            return list(value)

    try:
        build_ready_futu_broker_gateway(
            expected_account_ids=["1001", "1002"],
            trd_env="REAL",
            backend_cls=Backend,
            client_cls=Client,
        )
    except FutuGatewayError as exc:
        assert "1002" not in str(exc)
        assert "****" in str(exc)
    else:
        raise AssertionError("expected broker identity readiness failure")


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

    class Client:
        def __init__(self, backend, **_kwargs):
            self.backend = backend

        @staticmethod
        def _unwrap(value):
            return value[1]

        @staticmethod
        def _rows(value):
            return list(value)

    try:
        build_ready_futu_broker_gateway(
            expected_account_ids=["1001"],
            trd_env="REAL",
            backend_cls=Backend,
            client_cls=Client,
        )
    except FutuGatewayError as exc:
        assert "not READY" in str(exc)
    else:
        raise AssertionError("missing readiness facts must fail closed")


def test_default_backend_quote_readiness_does_not_construct_trade_context(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway

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
