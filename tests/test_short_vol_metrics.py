from __future__ import annotations

import math


def test_compute_realized_volatility_snapshot_uses_weighted_windows() -> None:
    from src.application.short_vol_metrics import compute_realized_volatility_snapshot

    closes = []
    price = 100.0
    for idx in range(130):
        price *= 1.0 + (0.01 if idx % 2 == 0 else -0.006)
        closes.append({"close": round(price, 6)})

    out = compute_realized_volatility_snapshot(closes)

    assert out.status == "ok"
    assert out.sample_count == 130
    assert out.rv_20 is not None and out.rv_20 > 0
    assert out.rv_60 is not None and out.rv_60 > 0
    assert out.rv_120 is not None and out.rv_120 > 0
    expected = (out.rv_20 * 0.50 + out.rv_60 * 0.30 + out.rv_120 * 0.20)
    assert math.isclose(out.rv_estimate or 0.0, expected, rel_tol=0.0, abs_tol=0.000002)


def test_compute_realized_volatility_snapshot_fails_closed_without_returns() -> None:
    from src.application.short_vol_metrics import compute_realized_volatility_snapshot

    out = compute_realized_volatility_snapshot([{"close": 100.0}])

    assert out.status == "missing"
    assert out.reason == "insufficient_close_prices"
    assert out.rv_estimate is None


def test_fetch_realized_volatility_snapshot_reads_history_pages() -> None:
    from datetime import date

    from src.application.short_vol_metrics import fetch_realized_volatility_snapshot

    class Gateway:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def request_history_kline(self, **kwargs):
            self.calls.append(dict(kwargs))
            start_idx = 0 if kwargs.get("page_req_key") is None else 70
            rows = []
            price = 100.0 + start_idx
            for idx in range(70):
                price *= 1.0 + (0.01 if idx % 2 == 0 else -0.006)
                rows.append({"close": round(price, 6)})
            return {"data": rows, "page_req_key": "next" if start_idx == 0 else None}

    gateway = Gateway()
    out = fetch_realized_volatility_snapshot(gateway, underlier_code="US.NVDA", trading_day=date(2026, 5, 26))

    assert out.status == "ok"
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["page_req_key"] is None
    assert gateway.calls[1]["page_req_key"] == "next"
