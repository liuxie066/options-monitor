"""Pure closed-market reference eligibility for option_candidates only.

The caller supplies a just-fetched underlier observation and an authoritative,
complete provider calendar. This policy neither fetches quotes nor grants live
scan eligibility. Returned timestamps are ISO 8601 UTC strings; failures expose
stable ValueError reason codes without replacing provider source times.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import math
from numbers import Real
from typing import Any, Mapping
from zoneinfo import ZoneInfo


_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}
_INACTIVE_MARKET_STATES = frozenset(
    {
        "CLOSED",
        "REST",
        "WAITING_OPEN",
        "PRE_MARKET_BEGIN",
        "PRE_MARKET_END",
        "AFTER_HOURS_BEGIN",
        "AFTER_HOURS_END",
        "AFTERNOON_END",
        "NIGHT_OPEN",
        "NIGHT_END",
        "NIGHT",
        "OVERNIGHT_BEGIN",
        "OVERNIGHT_END",
        "OVERNIGHT",
    }
)
_SESSION_HOURS = {
    "US": {
        "WHOLE": ((time(9, 30), time(16)),),
        "MORNING": ((time(9, 30), time(13)),),
    },
    "HK": {
        "WHOLE": ((time(9, 30), time(12)), (time(13), time(16))),
        "MORNING": ((time(9, 30), time(12)),),
        "AFTERNOON": ((time(13), time(16)),),
    },
}


def _calendar_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("calendar_invalid")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise ValueError("calendar_invalid") from None
    if day.isoformat() != value:
        raise ValueError("calendar_invalid")
    return day


def _calendar_intervals(
    calendar: Mapping[str, Any],
    *,
    market: str,
    source_date: date,
    current_date: date,
) -> list[tuple[date, datetime, datetime]]:
    if not isinstance(calendar, Mapping):
        raise ValueError("calendar_invalid")
    if calendar.get("coverage_complete") is not True or calendar.get("pagination_complete") is not True:
        raise ValueError("calendar_incomplete")
    request_start = _calendar_date(calendar.get("request_start"))
    request_end = _calendar_date(calendar.get("request_end"))
    if request_start > request_end:
        raise ValueError("calendar_invalid")
    if request_start >= source_date or request_end < current_date:
        raise ValueError("calendar_coverage_insufficient")
    sessions = calendar.get("sessions")
    if not isinstance(sessions, Mapping):
        raise ValueError("calendar_invalid")
    intervals = []
    for raw_day, day_type in sessions.items():
        day = _calendar_date(raw_day)
        if not request_start <= day <= request_end:
            raise ValueError("calendar_invalid")
        if not isinstance(day_type, str) or day_type not in {"WHOLE", "MORNING", "AFTERNOON"}:
            raise ValueError("calendar_invalid")
        if day_type not in _SESSION_HOURS[market]:
            raise ValueError("calendar_day_type_unsupported")
        for opening, closing in _SESSION_HOURS[market][day_type]:
            start = datetime.combine(day, opening, _MARKET_TIMEZONES[market]).astimezone(timezone.utc)
            end = datetime.combine(day, closing, _MARKET_TIMEZONES[market]).astimezone(timezone.utc)
            intervals.append((day, start, end))
    return sorted(intervals)


def resolve_closed_market_reference(
    *,
    observation: Mapping[str, Any],
    calendar: Mapping[str, Any],
    now_utc: datetime,
) -> dict[str, Any]:
    """Resolve the latest reference day and its exclusive next-opening cutoff.

    Eligibility requires an explicitly zoned source timestamp on the latest
    provider trading day whose first interval has opened, at or after that
    opening and no later than now. A scheduled regular interval always blocks
    reference use, even if the provider state still says CLOSED. During HK lunch
    the morning's reference expires at 13:00; close-auction source updates after
    the regular close remain eligible once the provider state is inactive.

    Calendar bounds must begin before the source's market-local date and cover
    now through a known future interval opening. Missing session dates inside
    those complete bounds are provider-declared non-trading days.
    """
    if not isinstance(now_utc, datetime):
        raise ValueError("now_utc_invalid")
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc_timezone_required")
    now = now_utc.astimezone(timezone.utc)
    if not isinstance(observation, Mapping):
        raise ValueError("observation_invalid")
    market = observation.get("market")
    if not isinstance(market, str) or market not in _MARKET_TIMEZONES:
        raise ValueError("market_invalid")
    code = observation.get("code")
    if not isinstance(code, str) or not code.startswith(f"{market}.") or not code[len(market) + 1:].strip():
        raise ValueError("underlier_code_invalid")
    if observation.get("sec_status") != "NORMAL" or observation.get("suspension") is not False:
        raise ValueError("security_not_normal")
    price = observation.get("last_price")
    if isinstance(price, bool) or not isinstance(price, Real):
        raise ValueError("underlier_price_invalid")
    try:
        price = float(price)
    except (ValueError, OverflowError):
        raise ValueError("underlier_price_invalid") from None
    if not math.isfinite(price) or price <= 0:
        raise ValueError("underlier_price_invalid")
    market_state = observation.get("market_state")
    if not isinstance(market_state, str) or market_state not in _INACTIVE_MARKET_STATES:
        raise ValueError("market_state_not_inactive")
    raw_observed_at = observation.get("observed_at_utc")
    if not isinstance(raw_observed_at, str):
        raise ValueError("observed_at_utc_invalid")
    try:
        observed_at = datetime.fromisoformat(raw_observed_at)
    except ValueError:
        raise ValueError("observed_at_utc_invalid") from None
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at_utc_timezone_required")
    observed_at = observed_at.astimezone(timezone.utc)
    if observed_at > now:
        raise ValueError("observation_in_future")
    source_date = observed_at.astimezone(_MARKET_TIMEZONES[market]).date()
    intervals = _calendar_intervals(
        calendar,
        market=market,
        source_date=source_date,
        current_date=now.astimezone(_MARKET_TIMEZONES[market]).date(),
    )
    if any(start <= now < end for _, start, end in intervals):
        raise ValueError("regular_session_active")
    elapsed = [(day, start) for day, start, _ in intervals if start <= now]
    if not elapsed:
        raise ValueError("reference_session_unavailable")
    reference_date = elapsed[-1][0]
    if source_date != reference_date:
        raise ValueError("reference_date_mismatch")
    first_open = next(start for day, start, _ in intervals if day == reference_date)
    if observed_at < first_open:
        raise ValueError("observation_before_open")
    valid_until = next((start for _, start, _ in intervals if start > now), None)
    if valid_until is None:
        raise ValueError("next_open_unavailable")
    return {
        "market": market,
        "reference_date": reference_date.isoformat(),
        "underlying_observed_at_utc": observed_at.isoformat(),
        "valid_until_utc": valid_until.isoformat(),
        "market_state": market_state,
    }
