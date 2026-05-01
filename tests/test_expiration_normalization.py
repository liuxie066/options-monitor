from __future__ import annotations

from datetime import datetime, timezone

from domain.domain.expiration_dates import expiration_business_today
from src.application.expiration_normalization import normalize_expiration_ymd


def test_normalize_expiration_ymd_uses_business_date_for_numeric_timestamps() -> None:
    assert normalize_expiration_ymd("1777564800000") == "2026-05-01"
    assert normalize_expiration_ymd("1781712000000") == "2026-06-18"
    assert normalize_expiration_ymd("1778803200000") == "2026-05-15"
    assert normalize_expiration_ymd("1777564800") == "2026-05-01"


def test_expiration_business_today_uses_business_timezone() -> None:
    assert expiration_business_today(datetime(2026, 4, 30, 16, 1, tzinfo=timezone.utc)).isoformat() == "2026-05-01"
    assert expiration_business_today(datetime(2026, 4, 30, 15, 59, tzinfo=timezone.utc)).isoformat() == "2026-04-30"
