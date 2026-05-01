from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any


EXPIRATION_DATE_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_MILLISECONDS_THRESHOLD = 10_000_000_000


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
