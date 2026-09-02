from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from domain.domain.performance.period import PeriodRequest, normalize_performance_period


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 22, 30, 45, 123000, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def test_normalize_mtd_and_ytd_windows() -> None:
    current = normalize_performance_period({"period": "ytd"}, report_now_ms=NOW_MS)
    historical = normalize_performance_period(
        {"period": "mtd", "as_of_date": "2026-06-15"},
        report_now_ms=NOW_MS,
    )

    assert current.requested_start_date == "2026-01-01"
    assert current.requested_end_date == "2026-07-17"
    assert current.effective_end_exclusive_at_ms == NOW_MS + 1
    assert current.statistic_days == Decimal(NOW_MS + 1 - _ms("2026-01-01T00:00:00")) / Decimal(86_400_000)
    assert current.status == "partial_current"
    assert historical.requested_start_date == "2026-06-01"
    assert historical.effective_end_exclusive_at_ms == _ms("2026-06-16T00:00:00")
    assert historical.status == "complete_past"


@pytest.mark.parametrize(
    "period_request",
    [
        PeriodRequest(period="month", month="2026-06"),
        PeriodRequest(period="year", year=2026),
        PeriodRequest(period="mtd", start_date="2026-06-01", end_date="2026-06-30"),
        PeriodRequest(period="invalid"),
    ],
)
def test_removed_period_inputs_are_rejected(period_request: PeriodRequest) -> None:
    with pytest.raises(ValueError, match="mtd or ytd|does not accept"):
        normalize_performance_period(period_request, report_now_ms=NOW_MS)


def test_as_of_and_cutoff_validation() -> None:
    with pytest.raises(ValueError, match="future"):
        normalize_performance_period(
            {"period": "mtd", "as_of_date": "2026-07-18"},
            report_now_ms=NOW_MS,
        )
    with pytest.raises(ValueError, match="inside"):
        normalize_performance_period(
            {"period": "mtd", "as_of_date": "2026-06-15"},
            report_now_ms=NOW_MS,
            cutoff_ms_override=_ms("2026-05-31T23:59:59"),
        )
