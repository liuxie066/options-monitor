"""Minimal tests for futu_gateway adapter (no futu/OpenD dependency)."""

from __future__ import annotations

from pathlib import Path

import pytest


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

        def get_stock_basicinfo(self, **kwargs):
            return kwargs

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

    rows = client.get_stock_basicinfo(
        market="US",
        stock_type="STOCK",
        code_list=["US.NVDA"],
    )

    assert rows == [{"code": "US.NVDA", "name": "NVIDIA"}]
    assert backend.quote.calls == [
        {
            "market": "US",
            "stock_type": "STOCK",
            "code_list": ["US.NVDA"],
        }
    ]


def test_futu_api_client_annotates_only_exact_native_expiry_order_shape() -> None:
    from src.infrastructure.futu_gateway import _FutuAPIClient

    base_row = {
        "order_id": "synthetic-expiry-order",
        "code": "HK.TCH260730P440000",
        "trd_side": "BUY_BACK",
        "order_type": "NORMAL",
        "order_status": "FILLED_ALL",
        "qty": 2.0,
        "price": 0.0,
        "dealt_qty": 2.0,
        "dealt_avg_price": 0.0,
        "last_err_msg": "",
        "remark": "",
        "create_time": "2026-07-30 19:25:32",
    }

    class FakeTrade:
        def history_order_list_query(self, **_kwargs):
            positive_row = dict(base_row, order_id="manual", price=0.01)
            wrong_day_row = dict(
                base_row,
                order_id="wrong-day",
                create_time="2026-07-29 16:00:00",
            )
            return 0, [base_row, positive_row, wrong_day_row], None

    class FakeBackend:
        def __init__(self) -> None:
            self.trade = FakeTrade()

        def _ensure_trade_client(self):
            return self.trade

    client = _FutuAPIClient(FakeBackend(), is_option_chain_cache_enabled=False)

    receipt = client.get_history_orders(acc_id="1001", trd_env="REAL")

    assert receipt["rows"][0]["order_origin"] == "broker_auto"
    assert (
        receipt["rows"][0]["order_origin_evidence"]
        == "futu_zero_price_expiry_shape.v1"
    )
    assert "order_origin" not in receipt["rows"][1]
    assert "order_origin" not in receipt["rows"][2]
    assert "order_origin" not in base_row


def test_get_trading_days_normalizes_market_label() -> None:
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

        def get_trading_days(self, **kwargs):
            return {"market": kwargs.get("market"), "start": kwargs.get("start")}

        def get_trading_days_with_receipt(self, **kwargs):
            return {"market": kwargs.get("market"), "coverage_complete": True}

    gw = build_futu_gateway(
        host="127.0.0.9",
        port=11119,
        is_option_chain_cache_enabled=True,
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )

    data = gw.get_trading_days(market="us", start="2026-08-01")
    assert data["market"] == "US"
    receipt = gw.get_trading_days_with_receipt(market="HK", start="2026-08-01")
    assert receipt["market"] == "HK"


def test_get_trading_days_rejects_unknown_market() -> None:
    import sys

    import pytest

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

        def get_trading_days(self, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("client must not be called for unknown market")

    gw = build_futu_gateway(
        host="127.0.0.9",
        port=11119,
        is_option_chain_cache_enabled=True,
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )

    with pytest.raises(Exception, match="unsupported trade date market"):
        gw.get_trading_days(market="MOON", start="2026-08-01")


def test_exact_expiration_option_terms_force_refresh_and_fail_closed() -> None:
    from src.infrastructure.futu_gateway import (
        FutuGatewayDataContractError,
        build_futu_gateway,
    )

    row = {
        "code": "HK.TCH261218P400000",
        "stock_owner": "HK.0700",
        "strike_time": "2026-12-18",
        "option_type": "PUT",
        "option_standard_type": "STANDARD",
        "strike_price": 400.0,
        "lot_size": 100,
        "currency": "HKD",
    }

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

    class FakeClient:
        def __init__(self, _backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.calls: list[dict[str, object]] = []
            self.rows = [row]

        def get_option_chain(self, **kwargs: object) -> list[dict[str, object]]:
            self.calls.append(dict(kwargs))
            return self.rows

    gateway = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)
    terms = gateway.get_exact_expiration_option_terms(
        code="hk.0700",
        expiration="2026-12-18",
        contract_symbol="hk.tch261218p400000",
    )

    assert terms == {
        "contract_symbol": "HK.TCH261218P400000",
        "stock_owner": "HK.0700",
        "expiration": "2026-12-18",
        "option_type": "PUT",
        "option_standard_type": "STANDARD",
        "strike": 400.0,
        "multiplier": 100,
        "currency": "HKD",
    }
    assert gateway.client.calls == [
        {
            "code": "HK.0700",
            "start": "2026-12-18",
            "end": "2026-12-18",
            "option_type": "PUT",
            "is_force_refresh": True,
        }
    ]

    gateway.client.rows = [row, dict(row)]
    with pytest.raises(FutuGatewayDataContractError):
        gateway.get_exact_expiration_option_terms(
            code="HK.0700",
            expiration="2026-12-18",
            contract_symbol="HK.TCH261218P400000",
        )


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


def test_gateway_request_history_kline_maps_time_key_to_sdk_date_time(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    from src.infrastructure.futu_gateway import build_futu_gateway

    fake_futu = SimpleNamespace(
        KLType=SimpleNamespace(K_DAY="k-day"),
        AuType=SimpleNamespace(QFQ="qfq"),
        KL_FIELD=SimpleNamespace(DATE_TIME="date-time", CLOSE="close"),
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

    gateway = build_futu_gateway(
        backend_cls=FakeBackend,
        client_cls=FakeClient,
    )

    gateway.request_history_kline(
        code="US.NVDA",
        start="2026-07-01",
        end="2026-08-06",
        ktype="K_DAY",
        autype="QFQ",
        fields=["time_key", "close"],
    )

    assert gateway.backend.quote.kwargs["fields"] == ["date-time", "close"]


def test_gateway_exact_expiration_close_requests_and_returns_bound_fact(
    monkeypatch,
) -> None:
    import sys
    from types import SimpleNamespace

    import pandas as pd

    from src.infrastructure.futu_gateway import build_futu_gateway

    fake_futu = SimpleNamespace(
        KLType=SimpleNamespace(K_DAY="k-day"),
        AuType=SimpleNamespace(NONE="none"),
        KL_FIELD=SimpleNamespace(DATE_TIME="date-time", CLOSE="close"),
    )
    monkeypatch.setitem(sys.modules, "futu", fake_futu)

    class FakeQuote:
        def __init__(self) -> None:
            self.calls = []
            self.result = (
                0,
                pd.DataFrame(
                    [
                        {
                            "code": "us.nvda",
                            "name": "NVIDIA",
                            "time_key": "2026-08-21 00:00:00",
                            "close": 900.25,
                        }
                    ]
                ),
                None,
            )

        def request_history_kline(self, **kwargs):
            self.calls.append(dict(kwargs))
            return self.result

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.quote = FakeQuote()

        def _ensure_quote_client(self):
            return self.quote

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend

    gateway = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)

    assert gateway.get_exact_expiration_close(
        code=" us.nvda ",
        expiration="2026-08-21",
    ) == {
        "code": "US.NVDA",
        "expiration": "2026-08-21",
        "close": 900.25,
    }
    assert gateway.backend.quote.calls == [
        {
            "code": "US.NVDA",
            "start": "2026-08-21",
            "end": "2026-08-21",
            "ktype": "k-day",
            "autype": "none",
            "fields": ["date-time", "close"],
            "max_count": 2,
            "page_req_key": None,
        }
    ]

    gateway.backend.quote.result = (
        0,
        pd.DataFrame(columns=["code", "name", "time_key", "close"]),
        None,
    )
    assert gateway.get_exact_expiration_close(
        code="US.NVDA",
        expiration="2026-08-21",
    ) is None


def test_gateway_exact_expiration_close_rejects_input_before_quote_access() -> None:
    import pytest

    from src.infrastructure.futu_gateway import FutuGatewayError, build_futu_gateway

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.quote_accesses = 0

        def _ensure_quote_client(self):
            self.quote_accesses += 1
            raise AssertionError("quote client must not be acquired")

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend

    gateway = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)
    invalid_inputs = [
        {"code": "", "expiration": "2026-08-21"},
        {"code": None, "expiration": "2026-08-21"},
        {"code": "US.NVDA", "expiration": None},
        {"code": "US.NVDA", "expiration": " 2026-08-21"},
        {"code": "US.NVDA", "expiration": "2026-8-21"},
        {"code": "US.NVDA", "expiration": "2026-02-30"},
    ]

    for kwargs in invalid_inputs:
        with pytest.raises(
            FutuGatewayError,
            match="get_exact_expiration_close failed",
        ):
            gateway.get_exact_expiration_close(**kwargs)

    assert gateway.backend.quote_accesses == 0


def test_gateway_exact_expiration_close_rejects_invalid_provider_facts() -> None:
    import pandas as pd
    import pytest

    from src.infrastructure.futu_gateway import (
        FutuGatewayError,
        FutuGatewayRateLimitError,
        build_futu_gateway,
    )

    valid_row = {
        "code": "US.NVDA",
        "time_key": "2026-08-21",
        "close": 900.25,
    }

    class FakeFrame:
        columns = ["code", "time_key", "close"]

        def __init__(self, records) -> None:
            self.records = records

        def to_dict(self, *, orient: str):
            assert orient == "records"
            return self.records

    class FakeQuote:
        result = None

        def request_history_kline(self, **_kwargs):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.quote = FakeQuote()

        def _ensure_quote_client(self):
            return self.quote

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend

    gateway = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)
    valid_frame = pd.DataFrame([valid_row])
    invalid_results = [
        None,
        (0, valid_frame),
        (False, valid_frame, None),
        (0.0, valid_frame, None),
        (1, "provider denied history request", None),
        (0, valid_frame, b"next-page"),
        (0, None, None),
        (0, [], None),
        (0, pd.DataFrame(columns=["code", "time_key"]), None),
        (0, FakeFrame({}), None),
        (0, FakeFrame(["not-an-object"]), None),
        (0, pd.DataFrame([valid_row, valid_row]), None),
        (0, pd.DataFrame([{**valid_row, "code": "US.TSLA"}]), None),
        (
            0,
            pd.DataFrame([{**valid_row, "time_key": "2026-08-20"}]),
            None,
        ),
        (
            0,
            pd.DataFrame([{**valid_row, "time_key": "2026-08-21T00:00:00"}]),
            None,
        ),
        (0, pd.DataFrame([{**valid_row, "close": "900.25"}]), None),
        (0, pd.DataFrame([{**valid_row, "close": True}]), None),
        (0, pd.DataFrame([{**valid_row, "close": 0.0}]), None),
        (0, pd.DataFrame([{**valid_row, "close": -1.0}]), None),
        (0, pd.DataFrame([{**valid_row, "close": float("nan")}]), None),
        (0, pd.DataFrame([{**valid_row, "close": float("inf")}]), None),
    ]

    for result in invalid_results:
        gateway.backend.quote.result = result
        with pytest.raises(
            FutuGatewayError,
            match="get_exact_expiration_close failed",
        ):
            gateway.get_exact_expiration_close(
                code="US.NVDA",
                expiration="2026-08-21",
            )

    gateway.backend.quote.result = RuntimeError("rate limit")
    with pytest.raises(FutuGatewayRateLimitError):
        gateway.get_exact_expiration_close(
            code="US.NVDA",
            expiration="2026-08-21",
        )


def test_gateway_history_kline_quota_returns_strict_compact_facts() -> None:
    from src.infrastructure.futu_gateway import build_futu_gateway

    class FakeQuote:
        def __init__(self) -> None:
            self.calls = []

        def get_history_kl_quota(self, **kwargs):
            self.calls.append(dict(kwargs))
            return (
                0,
                (
                    2,
                    98,
                    [
                        {
                            "code": "us.nvda",
                            "name": "NVIDIA",
                            "request_time": "2026-08-15 09:31:00",
                        },
                        {
                            "code": "HK.00700",
                            "name": "Tencent",
                            "request_time": "2026-08-14 15:59:00",
                        },
                    ],
                ),
            )

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.quote = FakeQuote()

        def _ensure_quote_client(self):
            return self.quote

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend

    gateway = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)

    assert gateway.get_history_kl_quota() == {
        "used_quota": 2,
        "remain_quota": 98,
        "detail_list": [
            {
                "code": "HK.00700",
                "request_time": "2026-08-14 15:59:00",
            },
            {
                "code": "US.NVDA",
                "request_time": "2026-08-15 09:31:00",
            },
        ],
    }
    assert gateway.backend.quote.calls == [{"get_detail": True}]


def test_gateway_history_kline_quota_rejects_invalid_provider_facts() -> None:
    import pytest

    from src.infrastructure.futu_gateway import FutuGatewayError, build_futu_gateway

    class FakeQuote:
        result = None

        def get_history_kl_quota(self, **_kwargs):
            return self.result

    class FakeBackend:
        def __init__(self, *, host: str, port: int) -> None:
            self.quote = FakeQuote()

        def _ensure_quote_client(self):
            return self.quote

    class FakeClient:
        def __init__(self, backend, *, is_option_chain_cache_enabled: bool) -> None:
            self.backend = backend

    gateway = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)
    valid_detail = {
        "code": "HK.00700",
        "request_time": "2026-08-14 15:59:00",
    }
    invalid_results = [
        None,
        (0,),
        (False, (0, 0, [])),
        (1, "provider denied quota request"),
        (0, None),
        (0, (True, 1, [valid_detail])),
        (0, ("1", 1, [valid_detail])),
        (0, (-1, 1, [])),
        (0, (1, 1, [])),
        (0, (1, 1, "not-a-list")),
        (0, (1, 1, [{}])),
        (
            0,
            (
                1,
                1,
                [{"code": "HK.00700", "request_time": "not-a-time"}],
            ),
        ),
        (
            0,
            (
                1,
                1,
                [{"code": "HK.00700", "request_time": "2026-02-30 09:00:00"}],
            ),
        ),
        (0, (2, 1, [valid_detail, dict(valid_detail)])),
    ]

    for result in invalid_results:
        gateway.backend.quote.result = result
        with pytest.raises(FutuGatewayError, match="get_history_kl_quota failed"):
            gateway.get_history_kl_quota()


def test_gateway_market_state_delegates_to_quote_client() -> None:
    from src.infrastructure.futu_gateway import build_futu_gateway

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

    gateway = build_futu_gateway(backend_cls=FakeBackend, client_cls=FakeClient)

    rows = gateway.get_market_state(["US.NVDA"])

    assert rows == [{"code": "US.NVDA", "market_state": "MORNING"}]
    assert gateway.client.calls == [{"code_list": ["US.NVDA"]}]


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
