from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.infrastructure.futu_history_deals import (
    OpenDHistoryDealClient,
    fetch_opend_history_deals,
    history_deal_query_dates,
)
from src.infrastructure.futu_gateway import FutuGatewayTransientError, FutuGatewayUnreachableError
from src.application.trades.backfill import _history_query_complete


@pytest.fixture(autouse=True)
def _open_port(monkeypatch):
    """Tests mock the futu SDK; keep the port pre-check passing."""

    from src.infrastructure import futu_gateway

    monkeypatch.setattr(futu_gateway, "port_open", lambda host, port: True)
    yield


def test_history_deal_query_dates_uses_hong_kong_trade_date_window() -> None:
    start_date, end_date, start_utc, end_utc = history_deal_query_dates(
        lookback_hours=6,
        now=datetime(2026, 6, 2, 18, 0, tzinfo=timezone.utc),
    )

    assert start_date == "2026-06-02 20:00:00"
    assert end_date == "2026-06-03 02:00:00"
    assert start_utc == "2026-06-02T12:00:00+00:00"
    assert end_utc == "2026-06-02T18:00:00+00:00"


@pytest.mark.parametrize(
    ("coverage", "status", "complete"),
    [
        ({"coverage_complete": True, "pagination_complete": True, "page_count": 2}, "complete", True),
        ({"coverage_complete": False, "pagination_complete": False, "page_count": 1}, "partial", False),
        ({"coverage_complete": True, "pagination_complete": True, "truncated": True}, "partial", False),
        ({"coverage_complete": True}, "unknown", False),
        ({}, "unknown", False),
    ],
)
@pytest.mark.parametrize("payload_rows", [[], [{"deal_id": "d1"}]])
def test_history_receipt_preserves_provider_coverage(coverage, status, complete, payload_rows):
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)
    client._gateway = SimpleNamespace(
        get_history_deals=lambda **_kwargs: {"retcode": 0, "rows": payload_rows, **coverage}
    )

    rows, diagnostics = client.fetch(
        futu_account_ids=["123"],
        lookback_hours=6,
        now=datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc),
    )

    receipt = diagnostics["account_results"][0]
    assert len(rows) == len(payload_rows)
    assert receipt["row_count"] == len(payload_rows)
    assert receipt["coverage_status"] == diagnostics["coverage_status"] == status
    assert receipt["requested_end_utc"] == "2026-06-03T06:00:00+00:00"
    assert receipt["covered_end_utc"] == (receipt["requested_end_utc"] if complete else None)
    assert receipt["trd_env"] == "REAL"
    assert _history_query_complete(diagnostics, expected_account_ids=["123"]) is complete


@pytest.mark.parametrize("error_type", [RuntimeError, FutuGatewayUnreachableError])
def test_history_receipt_preserves_successful_account_when_another_fails(error_type):
    def query(**kwargs):
        if kwargs["acc_id"] == 456:
            raise error_type("query failed")
        return {"retcode": 0, "rows": [{"deal_id": "d1"}], "coverage_complete": True, "pagination_complete": True}

    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)
    client._gateway = SimpleNamespace(get_history_deals=query, close=lambda: None)
    rows, diagnostics = client.fetch(
        futu_account_ids=["123", "456"], lookback_hours=6,
        now=datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    assert diagnostics["coverage_status"] == "partial"
    assert diagnostics["account_results"][0]["coverage_status"] == "complete"
    assert diagnostics["account_results"][1]["coverage_status"] == "unknown"
    assert diagnostics["account_results"][1]["error"] == "query failed"
    assert not _history_query_complete(diagnostics, expected_account_ids=["123", "456"])


@pytest.mark.parametrize("diagnostics", [{}, {"account_results": []}, {"account_results": [{"futu_account_id": "123", "ret": 0}]}])
def test_history_query_missing_receipt_is_unknown(diagnostics):
    assert not _history_query_complete(diagnostics, expected_account_ids=["123"])


def test_history_query_cannot_hide_a_failed_or_missing_account():
    success = {"futu_account_id": "123", "ret": 0, "coverage_status": "complete", "coverage_complete": True, "pagination_complete": True}
    assert not _history_query_complete({"account_results": [success]}, expected_account_ids=["123", "456"])
    assert not _history_query_complete(
        {"account_results": [success, {"futu_account_id": "123", "ret": -1}]},
        expected_account_ids=["123"],
    )


@pytest.mark.parametrize("field,value", [
    ("futu_account_id", "456"), ("acc_id", "456"),
    ("environment", "SIMULATE"), ("trd_env", "SIMULATE"),
    ("broker_account_id", "futu:REAL:456"),
    ("external_id_namespace", "other.deal"),
    ("external_order_namespace", "other.order"),
])
def test_history_adapter_rejects_conflicting_source_identity(field, value):
    original = {"deal_id": "d1", "order_id": "o1", field: value}
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)
    client._gateway = SimpleNamespace(
        get_history_deals=lambda **_kwargs: {
            "retcode": 0, "rows": [original], "coverage_complete": True, "pagination_complete": True,
        },
    )
    rows, diagnostics = client.fetch(futu_account_ids=["123"], lookback_hours=6)

    assert rows == []
    receipt = diagnostics["account_results"][0]
    assert receipt["coverage_status"] == "partial"
    assert receipt["covered_end_utc"] is None
    assert receipt["row_count"] == 1
    assert receipt["accepted_row_count"] == 0
    assert receipt["rejected_rows"][0]["payload"] == original
    assert field in receipt["rejected_rows"][0]["fields"]
    assert not _history_query_complete(diagnostics, expected_account_ids=["123"])


def test_history_adapter_binds_namespaces_from_provider_scope():
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)
    client._gateway = SimpleNamespace(get_history_deals=lambda **_kwargs: {
        "retcode": 0, "rows": [{"deal_id": "d1", "order_id": "o1"}],
        "coverage_complete": True, "pagination_complete": True,
    })
    rows, diagnostics = client.fetch(futu_account_ids=["123"], lookback_hours=6)
    assert rows[0]["environment"] == "REAL"
    assert rows[0]["broker_account_id"] == "futu:REAL:123"
    assert rows[0]["external_id_namespace"] == "futu.deal"
    assert rows[0]["external_order_namespace"] == "futu.order"
    assert diagnostics["coverage_status"] == "complete"


def test_fetch_opend_history_deals_adds_account_fields_and_diagnostics(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeData:
        def to_dict(self, orient: str) -> list[dict]:
            assert orient == "records"
            return [{"deal_id": "deal-1", "code": "HK.TCH260605P440000"}]

    class _FakeContext:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        def history_deal_list_query(self, **kwargs):
            calls.append({"query": kwargs})
            return 0, _FakeData()

        def close(self):
            calls.append({"closed": True})

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            OpenSecTradeContext=_FakeContext,
            TrdEnv=SimpleNamespace(REAL="REAL"),
            RET_OK=0,
        ),
    )

    rows, diagnostics = fetch_opend_history_deals(
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["123"],
        lookback_hours=6,
        now=datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc),
    )

    assert rows == [
        {
            "deal_id": "deal-1",
            "code": "HK.TCH260605P440000",
            "futu_account_id": "123",
            "trd_acc_id": "123",
            "environment": "REAL",
            "broker_account_id": "futu:REAL:123",
            "external_id_namespace": "futu.deal",
        }
    ]
    assert diagnostics["start_date"] == "2026-06-03 08:00:00"
    assert diagnostics["end_date"] == "2026-06-03 14:00:00"
    receipt = diagnostics["account_results"][0]
    assert receipt["futu_account_id"] == "123"
    assert receipt["ret"] == 0
    assert receipt["row_count"] == 1
    assert receipt["coverage_status"] == "complete"
    assert receipt["coverage_complete"] is True
    assert receipt["pagination_complete"] is True
    assert receipt["page_count"] == 1
    assert receipt["covered_start_utc"] == diagnostics["window_start_utc"]
    assert receipt["covered_end_utc"] == diagnostics["window_end_utc"]
    assert receipt["request_id"].startswith(diagnostics["request_id"])
    assert calls[0] == {"init": {"host": "127.0.0.1", "port": 11111}}
    assert calls[-1] == {"closed": True}


def test_fetch_opend_history_deals_skips_non_numeric_account_ids(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeContext:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        def history_deal_list_query(self, **kwargs):
            calls.append({"query": kwargs})
            return 0, SimpleNamespace(to_dict=lambda orient=None: [])

        def close(self):
            calls.append({"closed": True})

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            OpenSecTradeContext=_FakeContext,
            TrdEnv=SimpleNamespace(REAL="REAL"),
            RET_OK=0,
        ),
    )

    rows, diagnostics = fetch_opend_history_deals(
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["REAL_123"],
        lookback_hours=6,
        now=datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc),
    )

    assert rows == []
    receipt = diagnostics["account_results"][0]
    assert receipt["futu_account_id"] == "REAL_123"
    assert receipt["ret"] is None
    assert receipt["row_count"] == 0
    assert receipt["skipped"] is True
    assert receipt["reason"] == "non_numeric_account_id"
    assert receipt["coverage_status"] == "unknown"
    assert calls == []


def test_history_deal_client_reuses_one_context_across_checks(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeContext:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        def history_deal_list_query(self, **kwargs):
            calls.append({"query": kwargs})
            return 0, SimpleNamespace(to_dict=lambda orient=None: [])

        def close(self):
            calls.append({"closed": True})

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            OpenSecTradeContext=_FakeContext,
            TrdEnv=SimpleNamespace(REAL="REAL"),
            RET_OK=0,
        ),
    )
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)

    for hour in (6, 7):
        client.fetch(
            futu_account_ids=["123"],
            lookback_hours=6,
            now=datetime(2026, 6, 3, hour, 0, tzinfo=timezone.utc),
        )
    client.close()

    assert len([item for item in calls if "init" in item]) == 1
    assert len([item for item in calls if "query" in item]) == 2
    assert calls[-1] == {"closed": True}


def test_history_deal_client_reopens_context_after_query_error(monkeypatch) -> None:
    calls: list[dict] = []
    query_count = 0

    class _FakeContext:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        def history_deal_list_query(self, **kwargs):
            nonlocal query_count
            query_count += 1
            calls.append({"query": kwargs})
            if query_count == 1:
                return -1, "OpenD disconnected"
            return 0, SimpleNamespace(to_dict=lambda orient=None: [])

        def close(self):
            calls.append({"closed": True})

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            OpenSecTradeContext=_FakeContext,
            TrdEnv=SimpleNamespace(REAL="REAL"),
            RET_OK=0,
        ),
    )
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)

    _rows, first = client.fetch(
        futu_account_ids=["123"],
        lookback_hours=6,
        now=datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc),
    )
    _rows, second = client.fetch(
        futu_account_ids=["123"],
        lookback_hours=6,
        now=datetime(2026, 6, 3, 7, 0, tzinfo=timezone.utc),
    )
    client.close()

    assert "OpenD disconnected" in first["account_results"][0]["error"]
    assert second["account_results"][0]["ret"] == 0
    assert len([item for item in calls if "init" in item]) == 2
    assert len([item for item in calls if "closed" in item]) == 2


def test_history_deal_client_normalizes_terminal_orders_and_order_fees(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeData:
        def __init__(self, rows: list[dict]) -> None:
            self.rows = rows

        def to_dict(self, orient: str) -> list[dict]:
            assert orient == "records"
            return self.rows

    class _FakeContext:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        def history_order_list_query(self, **kwargs):
            calls.append({"orders": kwargs})
            return 0, _FakeData(
                [{"order_id": "o1", "order_status": "FILLED_ALL", "dealt_qty": 2, "currency": "USD"}]
            )

        def order_fee_query(self, **kwargs):
            calls.append({"fees": kwargs})
            return 0, _FakeData([{"order_id": "o1", "fee_amount": 0, "fee_details": {"commission": 0}}])

        def close(self):
            calls.append({"closed": True})

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            OpenSecTradeContext=_FakeContext,
            TrdEnv=SimpleNamespace(REAL="REAL"),
            RET_OK=0,
        ),
    )
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)

    orders, _ = client.fetch_terminal_orders(
        futu_account_id="123",
        order_ids=["o1"],
        start="2026-05-01 00:00:00",
        end="2026-05-02 00:00:00",
    )
    fees, _ = client.fetch_order_fees(futu_account_id="123", order_ids=["o1"])
    client.close()

    assert orders["o1"] == {
        "provider": "opend",
        "futu_account_id": "123",
        "order_id": "o1",
        "status": "terminal_with_fill",
        "dealt_qty": "2.000000",
        "currency": "USD",
    }
    assert fees["o1"]["fee_amount"] == "0.000000"
    assert calls[1]["orders"]["trd_env"] == "REAL"
    assert calls[1]["orders"]["acc_id"] == 123
    assert calls[2]["fees"] == {
        "order_id_list": ["o1"],
        "trd_env": "REAL",
        "acc_id": 123,
    }


def test_exact_terminal_order_uses_current_query_then_narrow_history_fallback(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    class _FakeData:
        def __init__(self, rows: list[dict]) -> None:
            self.rows = rows

        def to_dict(self, orient: str) -> list[dict]:
            assert orient == "records"
            return self.rows

    class _FakeContext:
        def __init__(self, **_kwargs):
            pass

        def order_list_query(self, **kwargs):
            calls.append({"current": kwargs})
            return 0, _FakeData([])

        def history_order_list_query(self, **kwargs):
            calls.append({"history": kwargs})
            return 0, _FakeData(
                [
                    {
                        "order_id": "other",
                        "order_status": "FILLED_ALL",
                        "dealt_qty": 99,
                        "currency": "USD",
                    },
                    {
                        "order_id": "o1",
                        "order_status": "FILLED_ALL",
                        "dealt_qty": 2,
                        "currency": "USD",
                    },
                ]
            )

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            OpenSecTradeContext=_FakeContext,
            TrdEnv=SimpleNamespace(REAL="REAL"),
            RET_OK=0,
        ),
    )
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)

    orders, diagnostics = client.fetch_terminal_orders(
        futu_account_id="123",
        order_ids=["o1"],
        start="2026-05-01 00:00:00",
        end="2026-05-02 00:00:00",
        exact=True,
    )

    assert set(orders) == {"o1"}
    assert diagnostics["query_source"] == "history_order"
    assert calls[0]["current"] == {
        "trd_env": "REAL",
        "acc_id": 123,
        "order_id": "o1",
        "refresh_cache": True,
    }
    assert calls[1]["history"]["start"] == "2026-05-01 00:00:00"
    assert calls[1]["history"]["end"] == "2026-05-02 00:00:00"


def test_exact_terminal_order_falls_back_when_current_query_fails() -> None:
    class _Gateway:
        def get_order_list(self, **_kwargs):
            raise FutuGatewayTransientError("temporary current-order failure")

        def get_history_orders(self, **_kwargs):
            return [
                {
                    "order_id": "o1",
                    "order_status": "FILLED_ALL",
                    "dealt_qty": 2,
                    "currency": "USD",
                }
            ]

        def close(self):
            pass

    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)
    client._gateway = _Gateway()

    orders, diagnostics = client.fetch_terminal_orders(
        futu_account_id="123",
        order_ids=["o1"],
        start="2026-05-01 00:00:00",
        end="2026-05-02 00:00:00",
        exact=True,
    )

    assert set(orders) == {"o1"}
    assert diagnostics["query_source"] == "history_order"
    assert diagnostics["current_order_error_type"] == "FutuGatewayTransientError"


def test_order_fee_query_rate_limit_is_shared_across_client_calls(monkeypatch) -> None:
    from src.infrastructure import futu_history_deals
    from src.infrastructure.futu_gateway import FutuGatewayRateLimitError

    class _FakeData:
        def to_dict(self, orient: str) -> list[dict]:
            assert orient == "records"
            return [{"order_id": "o1", "fee_amount": 1, "fee_details": {}}]

    class _FakeContext:
        def __init__(self, **_kwargs):
            pass

        def order_fee_query(self, **_kwargs):
            return 0, _FakeData()

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            OpenSecTradeContext=_FakeContext,
            TrdEnv=SimpleNamespace(REAL="REAL"),
            RET_OK=0,
        ),
    )
    monkeypatch.setattr(futu_history_deals.time, "monotonic", lambda: 100.0)
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)

    for _ in range(10):
        client.fetch_order_fees(futu_account_id="123", order_ids=["o1"])
    with pytest.raises(FutuGatewayRateLimitError):
        client.fetch_order_fees(futu_account_id="123", order_ids=["o1"])


def test_backfill_raises_typed_unreachable_when_port_closed(monkeypatch) -> None:
    from src.infrastructure import futu_gateway
    from src.infrastructure.futu_gateway import FutuGatewayUnreachableError

    monkeypatch.setattr(futu_gateway, "port_open", lambda host, port: False)
    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(
            OpenSecTradeContext=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("closed port must not construct a trade context")
            ),
            RET_OK=0,
        ),
    )

    client = OpenDHistoryDealClient(host="127.0.0.9", port=11119)
    with pytest.raises(FutuGatewayUnreachableError):
        client.fetch(
            futu_account_ids=["123"],
            lookback_hours=6,
            now=datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc),
        )
