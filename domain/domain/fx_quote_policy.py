"""Shared RMB quote-session policy for current capacity and historical booking."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_HOURS_SOURCE = "https://www.chinamoney.com.cn/chinese/mgwhcphjy/"
HOLIDAY_SOURCE_2026 = "https://big5.www.gov.cn/gate/big5/www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm"
CALENDAR_EVIDENCE = {
    "year": 2026,
    "market_hours_source": MARKET_HOURS_SOURCE,
    "holiday_source": HOLIDAY_SOURCE_2026,
}
# CFETS weekends stay closed, including government make-up working days.
_HOLIDAYS_2026 = (
    (date(2026, 1, 1), date(2026, 1, 3)),
    (date(2026, 2, 15), date(2026, 2, 23)),
    (date(2026, 4, 4), date(2026, 4, 6)),
    (date(2026, 5, 1), date(2026, 5, 5)),
    (date(2026, 6, 19), date(2026, 6, 21)),
    (date(2026, 9, 25), date(2026, 9, 27)),
    (date(2026, 10, 1), date(2026, 10, 7)),
)


def _market_day(day: date) -> bool | None:
    if day.year != 2026:
        return None
    return day.weekday() < 5 and not any(start <= day <= end for start, end in _HOLIDAYS_2026)


def quote_session_start(at: datetime) -> datetime | None:
    local = at.astimezone(SHANGHAI)
    for day in (local.date(), local.date() - timedelta(days=1)):
        if _market_day(day) is not True:
            continue
        start = datetime.combine(day, time(9, 30), SHANGHAI)
        if start <= local < start + timedelta(hours=17, minutes=30):
            return start
    return None


def _last_session_start(at: datetime) -> datetime | None:
    local = at.astimezone(SHANGHAI)
    day = local.date()
    while day.year == 2026:
        if _market_day(day):
            start = datetime.combine(day, time(9, 30), SHANGHAI)
            if start <= local:
                return start
        day -= timedelta(days=1)
    return None


def quote_quality(quoted: datetime | None, *, at: datetime) -> tuple[str, str]:
    """Evaluate the original quote at a consumption/event instant, without I/O."""
    if quoted is None or quoted.tzinfo is None or at.tzinfo is None or quoted > at or at.astimezone(SHANGHAI).year != 2026:
        return "unavailable", "calendar_or_timestamp_unknown"
    session = quote_session_start(quoted)
    latest = _last_session_start(at)
    if session is None or latest is None or session != latest:
        return "unavailable", "trading_session_gap"
    if quote_session_start(at) == latest:
        return ("fresh", "ok") if at - quoted <= timedelta(hours=24) else ("unavailable", "stale_quote")
    day = (latest + timedelta(days=1)).date()
    has_full_closure = False
    while day <= at.astimezone(SHANGHAI).date():
        market_day = _market_day(day)
        if market_day is None:
            return "unavailable", "calendar_unknown"
        has_full_closure |= not market_day
        day += timedelta(days=1)
    if has_full_closure:
        return "holiday_carried", "verified_market_closure"
    return ("fresh", "ok") if at - quoted <= timedelta(hours=24) else ("unavailable", "stale_quote")
