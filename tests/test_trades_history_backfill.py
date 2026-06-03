from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from src.application.trades.history_backfill import fetch_opend_history_deals, history_deal_query_dates


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
