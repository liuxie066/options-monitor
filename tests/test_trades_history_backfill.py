from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.application.trades.history_backfill import (
    OpenDHistoryDealClient,
    fetch_opend_history_deals,
    history_deal_query_dates,
)


@pytest.fixture(autouse=True)
def _open_port(monkeypatch):
    """Tests mock the futu SDK; keep the port pre-check passing."""

    from src.application.trades import history_backfill as mod

    monkeypatch.setattr(mod, "port_open", lambda host, port: True)
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
        }
    ]
    assert diagnostics["start_date"] == "2026-06-03 08:00:00"
    assert diagnostics["end_date"] == "2026-06-03 14:00:00"
    assert diagnostics["account_results"] == [{"futu_account_id": "123", "ret": 0, "row_count": 1}]
    assert calls[0] == {"init": {"host": "127.0.0.1", "port": 11111}}
    assert calls[-1] == {"closed": True}


def test_fetch_opend_history_deals_skips_non_numeric_account_ids(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeContext:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        def history_deal_list_query(self, **kwargs):
            calls.append({"query": kwargs})
            return 0, SimpleNamespace(to_dict=lambda _orient: [])

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
    assert diagnostics["account_results"] == [
        {
            "futu_account_id": "REAL_123",
            "ret": None,
            "row_count": 0,
            "skipped": True,
            "reason": "non_numeric_account_id",
        }
    ]
    assert calls == [{"init": {"host": "127.0.0.1", "port": 11111}}, {"closed": True}]


def test_history_deal_client_reuses_one_context_across_checks(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeContext:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        def history_deal_list_query(self, **kwargs):
            calls.append({"query": kwargs})
            return 0, SimpleNamespace(to_dict=lambda _orient: [])

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
            return 0, SimpleNamespace(to_dict=lambda _orient: [])

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

    assert first["account_results"][0]["error"] == "OpenD disconnected"
    assert second["account_results"][0]["ret"] == 0
    assert len([item for item in calls if "init" in item]) == 2
    assert len([item for item in calls if "closed" in item]) == 2


def test_backfill_raises_typed_unreachable_when_port_closed(monkeypatch) -> None:
    from src.application.trades import history_backfill as mod
    from src.infrastructure.futu_gateway import FutuGatewayUnreachableError

    monkeypatch.setattr(mod, "port_open", lambda host, port: False)

    client = OpenDHistoryDealClient(host="127.0.0.9", port=11119)
    with pytest.raises(FutuGatewayUnreachableError):
        client._context()
