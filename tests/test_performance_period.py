from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from domain.domain.performance.period import PeriodRequest, normalize_period

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 17, 22, 30, 45, 123000, tzinfo=TZ)
NOW_MS = int(NOW.timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def test_normalize_ytd_current_uses_local_year_and_now_exclusive() -> None:
    out = normalize_period({"period": "ytd"}, now_ms=NOW_MS)

    assert out.requested_start_date == "2026-01-01"
    assert out.requested_end_date == "2026-07-17"
    assert out.effective_start_at_ms == _ms("2026-01-01T00:00:00")
    assert out.effective_end_exclusive_at_ms == NOW_MS + 1
    assert out.valuation_open_at_ms == out.effective_start_at_ms - 1
    assert out.valuation_end_at_ms == NOW_MS
    assert out.status == "partial_current"


def test_normalize_mtd_past_as_of_uses_next_local_midnight() -> None:
    out = normalize_period({"period": "mtd", "as_of_date": "2026-06-15"}, now_ms=NOW_MS)

    assert out.requested_start_date == "2026-06-01"
    assert out.requested_end_date == "2026-06-15"
    assert out.effective_end_exclusive_at_ms == _ms("2026-06-16T00:00:00")
    assert out.status == "complete_past"


def test_normalize_complete_month_and_year() -> None:
    month = normalize_period(PeriodRequest(period="month", month="2026-06"), now_ms=NOW_MS)
    year = normalize_period({"period": "year", "year": 2025}, now_ms=NOW_MS)

    assert (month.requested_start_date, month.requested_end_date) == ("2026-06-01", "2026-06-30")
    assert month.effective_end_exclusive_at_ms == _ms("2026-07-01T00:00:00")
    assert (year.requested_start_date, year.requested_end_date) == ("2025-01-01", "2025-12-31")
    assert year.effective_end_exclusive_at_ms == _ms("2026-01-01T00:00:00")


def test_current_month_and_year_are_partial_at_same_now() -> None:
    month = normalize_period({"period": "month", "month": "2026-07"}, now_ms=NOW_MS)
    year = normalize_period({"period": "year", "year": 2026}, now_ms=NOW_MS)

    assert month.effective_end_exclusive_at_ms == NOW_MS + 1
    assert year.effective_end_exclusive_at_ms == NOW_MS + 1
    assert month.status == year.status == "partial_current"


def test_range_is_inclusive_dates_and_half_open_instants() -> None:
    out = normalize_period(
        {"period": "range", "start_date": "2026-04-01", "end_date": "2026-06-30"},
        now_ms=NOW_MS,
    )

    assert out.effective_start_at_ms == _ms("2026-04-01T00:00:00")
    assert out.effective_end_exclusive_at_ms == _ms("2026-07-01T00:00:00")
    assert out.contains(_ms("2026-06-30T23:59:59"))
    assert not out.contains(_ms("2026-07-01T00:00:00"))


def test_cutoff_override_is_internal_and_inside_period() -> None:
    cutoff = _ms("2026-06-15T12:00:00")
    out = normalize_period(
        {"period": "month", "month": "2026-06"},
        now_ms=NOW_MS,
        cutoff_ms_override=cutoff,
    )

    assert out.effective_end_exclusive_at_ms == cutoff + 1
    assert out.valuation_end_at_ms == cutoff
    assert out.status == "partial_cutoff"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"period": "nope"}, "period must be one of"),
        ({"period": "mtd", "as_of_date": "2026-07-18"}, "future"),
        ({"period": "month", "month": "2026-13"}, "YYYY-MM"),
        ({"period": "month", "month": "2026-08"}, "future"),
        ({"period": "year", "year": 2027}, "future"),
        ({"period": "range", "start_date": "2026-06-02", "end_date": "2026-06-01"}, "on or before"),
        ({"period": "range", "start_date": "2026-06-01", "end_date": "2026-07-18"}, "future"),
        ({"period": "month", "month": "2026-06", "year": 2026}, "does not accept"),
    ],
)
def test_invalid_period_inputs_are_rejected(payload: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_period(payload, now_ms=NOW_MS)


def test_cutoff_override_rejects_outside_or_future() -> None:
    with pytest.raises(ValueError, match="inside"):
        normalize_period({"period": "month", "month": "2026-06"}, now_ms=NOW_MS, cutoff_ms_override=_ms("2026-05-31T23:59:59"))
    with pytest.raises(ValueError, match="future"):
        normalize_period({"period": "mtd"}, now_ms=NOW_MS, cutoff_ms_override=NOW_MS + 1)


def test_direct_period_request_cannot_bypass_conditional_field_validation() -> None:
    with pytest.raises(ValueError, match="does not accept"):
        normalize_period(PeriodRequest(period="month", month="2026-06", year=2025), now_ms=NOW_MS)
    with pytest.raises(ValueError, match="period must be one of"):
        normalize_period(PeriodRequest(period="invalid"), now_ms=NOW_MS)
