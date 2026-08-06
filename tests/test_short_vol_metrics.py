from __future__ import annotations

import json
import math


def _sessions_around(anchor, *, before: int, after: int):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    days = []
    cursor = anchor - timedelta(days=1)
    while len(days) < before:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    past = list(reversed(days))
    future = []
    cursor = anchor
    while len(future) < after:
        if cursor.weekday() < 5:
            future.append(cursor)
        cursor += timedelta(days=1)
    return past, future


def _prices(sessions):  # type: ignore[no-untyped-def]
    price = 100.0
    values = {}
    for index, session in enumerate(sessions):
        price *= 1.0 + (0.008 if index % 2 == 0 else -0.005)
        values[session] = round(price, 8)
    return values


class _Gateway:
    def __init__(self, *, calendar, prices):  # type: ignore[no-untyped-def]
        self.calendar = list(calendar)
        self.prices = dict(prices)
        self.calendar_calls = []
        self.history_calls = []

    def get_trading_days(self, **kwargs):  # type: ignore[no-untyped-def]
        from datetime import date

        self.calendar_calls.append(dict(kwargs))
        start = date.fromisoformat(kwargs["start"])
        end = date.fromisoformat(kwargs["end"])
        return [
            {"time": session.isoformat(), "trade_date_type": "WHOLE"}
            for session in self.calendar
            if start <= session <= end
        ]

    def request_history_kline(self, **kwargs):  # type: ignore[no-untyped-def]
        from datetime import date

        self.history_calls.append(dict(kwargs))
        start = date.fromisoformat(kwargs["start"])
        end = date.fromisoformat(kwargs["end"])
        return {
            "data": [
                {"time_key": session.isoformat(), "close": close}
                for session, close in sorted(self.prices.items())
                if start <= session <= end
            ],
            "page_req_key": None,
        }


def test_compute_realized_volatility_snapshot_uses_dte_matched_windows() -> None:
    from src.application.short_vol_metrics import (
        compute_realized_volatility_snapshot,
        realized_volatility_estimate_for_dte,
    )

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
    assert out.rv_estimate is None
    expected_30 = out.rv_20 * 0.70 + out.rv_60 * 0.30
    expected_60 = out.rv_20 * 0.30 + out.rv_60 * 0.50 + out.rv_120 * 0.20
    expected_90 = out.rv_20 * 0.20 + out.rv_60 * 0.40 + out.rv_120 * 0.40
    assert math.isclose(realized_volatility_estimate_for_dte(dte=30, rv_20=out.rv_20, rv_60=out.rv_60, rv_120=out.rv_120) or 0.0, expected_30, rel_tol=0.0, abs_tol=0.000002)
    assert math.isclose(realized_volatility_estimate_for_dte(dte=60, rv_20=out.rv_20, rv_60=out.rv_60, rv_120=out.rv_120) or 0.0, expected_60, rel_tol=0.0, abs_tol=0.000002)
    assert math.isclose(realized_volatility_estimate_for_dte(dte=90, rv_20=out.rv_20, rv_60=out.rv_60, rv_120=out.rv_120) or 0.0, expected_90, rel_tol=0.0, abs_tol=0.000002)


def test_dte_matched_rv_fails_closed_when_required_window_is_missing() -> None:
    from src.application.short_vol_metrics import realized_volatility_estimate_for_dte

    assert realized_volatility_estimate_for_dte(dte=30, rv_20=0.2, rv_60=None, rv_120=0.3) is None
    assert realized_volatility_estimate_for_dte(dte=60, rv_20=0.2, rv_60=0.22, rv_120=None) is None


def test_compute_realized_volatility_snapshot_fails_closed_without_returns() -> None:
    from src.application.short_vol_metrics import compute_realized_volatility_snapshot

    out = compute_realized_volatility_snapshot([{"close": 100.0}])

    assert out.status == "missing"
    assert out.reason == "insufficient_close_prices"
    assert out.rv_estimate is None


def test_fetch_realized_volatility_snapshot_reads_history_pages() -> None:
    from datetime import date, timedelta

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
                rows.append(
                    {
                        "time_key": (
                            date(2025, 10, 1)
                            + timedelta(days=start_idx + idx)
                        ).isoformat(),
                        "close": round(price, 6),
                    }
                )
            return {"data": rows, "page_req_key": "next" if start_idx == 0 else None}

    gateway = Gateway()
    out = fetch_realized_volatility_snapshot(gateway, underlier_code="US.NVDA", trading_day=date(2026, 5, 26))

    assert out.status == "ok"
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["page_req_key"] is None
    assert gateway.calls[1]["page_req_key"] == "next"


def test_term_matched_rv_uses_remaining_sessions_and_excludes_current_bar(
    tmp_path,
) -> None:
    from datetime import date

    from src.application.short_vol_metrics import fetch_realized_volatility_snapshot

    trading_day = date(2026, 8, 6)
    past, future = _sessions_around(trading_day, before=150, after=70)
    prices = _prices(past)
    prices[trading_day] = 999.0
    expiration = future[39]
    gateway = _Gateway(calendar=[*past, *future], prices=prices)

    snapshot = fetch_realized_volatility_snapshot(
        gateway,
        underlier_code="US.NVDA",
        trading_day=trading_day,
        market="US",
        expirations=[expiration.isoformat()],
        base_dir=tmp_path,
    )
    observation = snapshot.term_matched[expiration.isoformat()]

    assert snapshot.status == "ok"
    assert observation.status == "ok"
    assert observation.remaining_sessions == 40
    assert observation.lookback_sessions == 40
    assert observation.input_close_session_count == 41
    assert observation.input_return_count == 40
    assert observation.input_end == past[-1].isoformat()
    assert observation.input_hash is not None
    assert len(observation.input_hash) == 64
    assert observation.term_matched_rv is not None
    assert observation.legacy_weighted_rv is not None
    assert observation.shadow_difference == round(
        observation.term_matched_rv - observation.legacy_weighted_rv,
        6,
    )
    assert gateway.history_calls[0]["autype"] == "QFQ"


def test_term_matched_rv_gap_is_scoped_to_dependent_expiry(tmp_path) -> None:
    from datetime import date

    from src.application.short_vol_metrics import fetch_realized_volatility_snapshot

    trading_day = date(2026, 8, 6)
    past, future = _sessions_around(trading_day, before=150, after=60)
    prices = _prices(past)
    prices.pop(past[-30])
    short_expiry = future[9]
    long_expiry = future[39]
    snapshot = fetch_realized_volatility_snapshot(
        _Gateway(calendar=[*past, *future], prices=prices),
        underlier_code="US.NVDA",
        trading_day=trading_day,
        market="US",
        expirations=[short_expiry.isoformat(), long_expiry.isoformat()],
        base_dir=tmp_path,
    )

    assert snapshot.status == "partial"
    assert snapshot.term_matched[short_expiry.isoformat()].status == "ok"
    unavailable = snapshot.term_matched[long_expiry.isoformat()]
    assert unavailable.status == "data_unavailable"
    assert unavailable.reason == "qfq_history_session_gap"
    assert unavailable.missing_sessions == (past[-30].isoformat(),)


def test_qfq_cache_incrementally_rechecks_last_five_sessions(tmp_path) -> None:
    from datetime import date

    from src.application.short_vol_metrics import fetch_realized_volatility_snapshot

    first_day = date(2026, 8, 6)
    past, future = _sessions_around(first_day, before=150, after=60)
    prices = _prices([*past, first_day])
    first = _Gateway(calendar=[*past, *future], prices=prices)
    expiration = future[19]
    first_snapshot = fetch_realized_volatility_snapshot(
        first,
        underlier_code="US.NVDA",
        trading_day=first_day,
        market="US",
        expirations=[expiration.isoformat()],
        base_dir=tmp_path,
    )
    assert first_snapshot.qfq_history_evidence["cache_status"] == "created"

    next_day = future[1]
    past2, future2 = _sessions_around(next_day, before=152, after=60)
    prices2 = dict(prices)
    second = _Gateway(calendar=[*past2, *future2], prices=prices2)
    second_snapshot = fetch_realized_volatility_snapshot(
        second,
        underlier_code="US.NVDA",
        trading_day=next_day,
        market="US",
        expirations=[future2[19].isoformat()],
        base_dir=tmp_path,
    )

    assert second_snapshot.qfq_history_evidence["cache_status"] == "refreshed"
    assert second_snapshot.qfq_history_evidence["revision_detected"] is False
    assert len(second.history_calls) == 1
    assert second.history_calls[0]["start"] > second.calendar_calls[0]["start"]


def test_qfq_revision_triggers_full_required_horizon_refresh(tmp_path) -> None:
    from datetime import date

    from src.application.short_vol_metrics import fetch_realized_volatility_snapshot

    trading_day = date(2026, 8, 6)
    past, future = _sessions_around(trading_day, before=150, after=60)
    prices = _prices(past)
    expiration = future[29]
    fetch_realized_volatility_snapshot(
        _Gateway(calendar=[*past, *future], prices=prices),
        underlier_code="HK.00700",
        trading_day=trading_day,
        market="HK",
        expirations=[expiration.isoformat()],
        base_dir=tmp_path,
    )
    revised = dict(prices)
    revised[past[-3]] = revised[past[-3]] * 0.95
    gateway = _Gateway(calendar=[*past, *future], prices=revised)

    snapshot = fetch_realized_volatility_snapshot(
        gateway,
        underlier_code="HK.00700",
        trading_day=trading_day,
        market="HK",
        expirations=[expiration.isoformat()],
        base_dir=tmp_path,
    )

    assert snapshot.status == "ok"
    assert snapshot.qfq_history_evidence["revision_detected"] is True
    assert len(gateway.history_calls) == 2
    assert gateway.history_calls[1]["start"] == gateway.calendar_calls[0]["start"]


def test_us_and_hk_calendars_produce_market_specific_remaining_sessions(
    tmp_path,
) -> None:
    from datetime import date

    from src.application.short_vol_metrics import fetch_realized_volatility_snapshot

    trading_day = date(2026, 8, 6)
    past, future = _sessions_around(trading_day, before=150, after=40)
    expiration = future[19]
    prices = _prices(past)
    us = fetch_realized_volatility_snapshot(
        _Gateway(calendar=[*past, *future], prices=prices),
        underlier_code="US.NVDA",
        trading_day=trading_day,
        market="US",
        expirations=[expiration.isoformat()],
        base_dir=tmp_path / "us",
    )
    hk_future = [session for index, session in enumerate(future) if index != 5]
    hk = fetch_realized_volatility_snapshot(
        _Gateway(calendar=[*past, *hk_future], prices=prices),
        underlier_code="HK.00700",
        trading_day=trading_day,
        market="HK",
        expirations=[expiration.isoformat()],
        base_dir=tmp_path / "hk",
    )

    assert us.term_matched[expiration.isoformat()].remaining_sessions == 20
    assert hk.term_matched[expiration.isoformat()].remaining_sessions == 19


def test_shorter_followup_request_preserves_earlier_cached_history(tmp_path) -> None:
    from datetime import date

    from src.application.short_vol_metrics import (
        fetch_realized_volatility_snapshot,
        qfq_history_cache_path,
    )

    trading_day = date(2026, 8, 6)
    past, future = _sessions_around(trading_day, before=360, after=180)
    prices = _prices(past)
    cache_path = qfq_history_cache_path(
        tmp_path,
        market="US",
        underlier_code="US.NVDA",
    )
    fetch_realized_volatility_snapshot(
        _Gateway(calendar=[*past, *future], prices=prices),
        underlier_code="US.NVDA",
        trading_day=trading_day,
        market="US",
        expirations=[future[159].isoformat()],
        base_dir=tmp_path,
    )
    long_cache = json.loads(cache_path.read_text(encoding="utf-8"))

    fetch_realized_volatility_snapshot(
        _Gateway(calendar=[*past, *future], prices=prices),
        underlier_code="US.NVDA",
        trading_day=trading_day,
        market="US",
        expirations=[future[19].isoformat()],
        base_dir=tmp_path,
    )
    short_cache = json.loads(cache_path.read_text(encoding="utf-8"))

    assert short_cache["rows"][0] == long_cache["rows"][0]
    assert len(short_cache["rows"]) == len(long_cache["rows"])


def test_invalid_qfq_cache_hash_forces_full_required_horizon_fetch(tmp_path) -> None:
    from datetime import date

    from src.application.short_vol_metrics import (
        fetch_realized_volatility_snapshot,
        qfq_history_cache_path,
    )

    trading_day = date(2026, 8, 6)
    past, future = _sessions_around(trading_day, before=150, after=40)
    prices = _prices(past)
    first_gateway = _Gateway(calendar=[*past, *future], prices=prices)
    expiration = future[19]
    fetch_realized_volatility_snapshot(
        first_gateway,
        underlier_code="US.NVDA",
        trading_day=trading_day,
        market="US",
        expirations=[expiration.isoformat()],
        base_dir=tmp_path,
    )
    cache_path = qfq_history_cache_path(
        tmp_path,
        market="US",
        underlier_code="US.NVDA",
    )
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["rows_hash"] = "0" * 64
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    gateway = _Gateway(calendar=[*past, *future], prices=prices)
    snapshot = fetch_realized_volatility_snapshot(
        gateway,
        underlier_code="US.NVDA",
        trading_day=trading_day,
        market="US",
        expirations=[expiration.isoformat()],
        base_dir=tmp_path,
    )

    assert snapshot.status == "ok"
    assert gateway.history_calls[0]["start"] == gateway.calendar_calls[0]["start"]
