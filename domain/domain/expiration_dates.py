from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


EXPIRATION_DATE_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_MILLISECONDS_THRESHOLD = 10_000_000_000
MARKET_TIMEZONES = {"US": "America/New_York", "HK": "Asia/Hong_Kong"}


def expiration_market_date(now: datetime, market: str | None) -> date | None:
    """Market date for relative expiry; unknown markets remain unavailable."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("market date requires timezone-aware time")
    zone = MARKET_TIMEZONES.get(str(market or "").strip().upper())
    return now.astimezone(ZoneInfo(zone)).date() if zone else None


def expiration_timestamp_to_date(value: Any) -> date | None:
    try:
        if value in (None, ""):
            return None
        raw = int(float(value))
        if raw <= 0:
            return None
        seconds = raw / 1000 if raw > _MILLISECONDS_THRESHOLD else raw
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .astimezone(EXPIRATION_DATE_TZ)
            .date()
        )
    except Exception:
        return None


def expiration_timestamp_to_ymd(value: Any) -> str | None:
    exp_date = expiration_timestamp_to_date(value)
    return exp_date.isoformat() if exp_date is not None else None


def expiration_business_today(now: datetime | None = None) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(EXPIRATION_DATE_TZ).date()
