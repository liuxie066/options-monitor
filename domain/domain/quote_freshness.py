from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class OptionQuoteFreshness:
    rank_eligible: bool
    status: str
    observed_at_utc: str | None
    age_seconds: float | None


_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}
_REGULAR_INTERVALS = {
    "US": ((time(9, 30), time(16, 0)),),
    "HK": ((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))),
}


def _parse_update_time(value: Any, *, market: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_MARKET_TIMEZONES[market])
    return parsed.astimezone(timezone.utc)


def _inside_interval(value: time, interval: tuple[time, time]) -> bool:
    start, end = interval
    return start <= value <= end


def _active_session(now_local: datetime, *, market: str) -> bool:
    if now_local.weekday() >= 5:
        return False
    return any(
        start <= now_local.time() < end
        for start, end in _REGULAR_INTERVALS[market]
    )


def _previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _latest_completed_session_date(now_local: datetime, *, market: str) -> date:
    if now_local.weekday() >= 5:
        candidate = now_local.date()
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate
    intervals = _REGULAR_INTERVALS[market]
    if now_local.time() < intervals[0][0]:
        return _previous_weekday(now_local.date())
    return now_local.date()


def evaluate_option_quote_freshness(
    *,
    market: str,
    update_time: Any,
    now_utc: datetime | None = None,
    max_age_seconds: int = 300,
) -> OptionQuoteFreshness:
    """Classify an OpenD option quote for ranking eligibility.

    Only a quote observed in the currently active regular session and no more
    than five minutes old is rankable. A latest completed-session quote remains
    distinguishable as observation data while the market is closed.
    """

    market_code = str(market or "").strip().upper()
    if market_code not in _MARKET_TIMEZONES:
        return OptionQuoteFreshness(False, "quote_market_missing", None, None)
    observed = _parse_update_time(update_time, market=market_code)
    if observed is None:
        return OptionQuoteFreshness(False, "quote_update_time_missing_or_invalid", None, None)
    now_value = now_utc or datetime.now(timezone.utc)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=timezone.utc)
    now_value = now_value.astimezone(timezone.utc)
    age_seconds = (now_value - observed).total_seconds()
    observed_local = observed.astimezone(_MARKET_TIMEZONES[market_code])
    now_local = now_value.astimezone(_MARKET_TIMEZONES[market_code])

    if age_seconds < 0:
        return OptionQuoteFreshness(
            False,
            "quote_update_time_in_future",
            observed.isoformat(),
            age_seconds,
        )
    if _active_session(now_local, market=market_code):
        same_session = (
            observed_local.date() == now_local.date()
            and observed_local.weekday() < 5
            and any(
                _inside_interval(observed_local.time(), interval)
                for interval in _REGULAR_INTERVALS[market_code]
            )
        )
        if same_session and age_seconds <= int(max_age_seconds):
            return OptionQuoteFreshness(
                True,
                "fresh_active_session",
                observed.isoformat(),
                age_seconds,
            )
        return OptionQuoteFreshness(
            False,
            "stale_during_active_session",
            observed.isoformat(),
            age_seconds,
        )

    latest_session_date = _latest_completed_session_date(now_local, market=market_code)
    is_regular_observation = (
        observed_local.date() == latest_session_date
        and observed_local.weekday() < 5
        and any(
            _inside_interval(observed_local.time(), interval)
            for interval in _REGULAR_INTERVALS[market_code]
        )
    )
    return OptionQuoteFreshness(
        False,
        (
            "latest_closed_session_observation"
            if is_regular_observation
            else "stale_closed_session_observation"
        ),
        observed.isoformat(),
        age_seconds,
    )
