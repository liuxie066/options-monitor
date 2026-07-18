from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

REPORTING_TIMEZONE = "Asia/Shanghai"
_PERIOD_KINDS = frozenset({"mtd", "ytd", "month", "year", "range"})


def _parse_date(value: Any, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _parse_month(value: Any) -> tuple[int, int]:
    raw = str(value or "").strip()
    if len(raw) != 7 or raw[4] != "-":
        raise ValueError("month must be YYYY-MM")
    try:
        parsed = date.fromisoformat(f"{raw}-01")
    except ValueError as exc:
        raise ValueError("month must be YYYY-MM") from exc
    return parsed.year, parsed.month


def _parse_year(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("year must be four digits")
    raw = str(value or "").strip()
    if len(raw) != 4 or not raw.isdigit():
        raise ValueError("year must be four digits")
    parsed = int(raw)
    if parsed < 1:
        raise ValueError("year must be four digits")
    return parsed


def _local_midnight_ms(value: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(value, time.min, tzinfo=tz).timestamp() * 1000)


def _next_month_start(year: int, month: int) -> date:
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


@dataclass(frozen=True)
class PeriodRequest:
    period: str = "mtd"
    as_of_date: date | str | None = None
    month: str | None = None
    year: int | str | None = None
    start_date: date | str | None = None
    end_date: date | str | None = None

    def validate(self) -> "PeriodRequest":
        period = str(self.period or "mtd").strip().lower()
        if period not in _PERIOD_KINDS:
            raise ValueError("period must be one of: month, mtd, range, year, ytd")
        relevant = {
            "mtd": {"as_of_date"},
            "ytd": {"as_of_date"},
            "month": {"month"},
            "year": {"year"},
            "range": {"start_date", "end_date"},
        }[period]
        values = {
            "as_of_date": self.as_of_date,
            "month": self.month,
            "year": self.year,
            "start_date": self.start_date,
            "end_date": self.end_date,
        }
        extras = sorted(field for field, value in values.items() if field not in relevant and value not in (None, ""))
        if extras:
            raise ValueError(f"period={period} does not accept: {', '.join(extras)}")
        return self

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "PeriodRequest":
        request = cls(
            period=str(payload.get("period") or "mtd").strip().lower(),
            as_of_date=payload.get("as_of_date"),
            month=payload.get("month"),
            year=payload.get("year"),
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
        )
        return request.validate()


@dataclass(frozen=True)
class PeriodWindow:
    kind: str
    reporting_timezone: str
    requested_start_date: str
    requested_end_date: str
    effective_start_at_ms: int
    effective_end_exclusive_at_ms: int
    valuation_open_at_ms: int
    valuation_end_at_ms: int
    status: str

    @property
    def is_current(self) -> bool:
        return self.status == "partial_current"

    def contains(self, timestamp_ms: int) -> bool:
        value = int(timestamp_ms)
        return self.effective_start_at_ms <= value < self.effective_end_exclusive_at_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reporting_timezone": self.reporting_timezone,
            "requested_start_date": self.requested_start_date,
            "requested_end_date": self.requested_end_date,
            "effective_start_at_ms": self.effective_start_at_ms,
            "effective_end_exclusive_at_ms": self.effective_end_exclusive_at_ms,
            "valuation_open_at_ms": self.valuation_open_at_ms,
            "valuation_end_at_ms": self.valuation_end_at_ms,
            "status": self.status,
        }


def normalize_period(
    request: PeriodRequest | Mapping[str, Any],
    *,
    now_ms: int | None = None,
    cutoff_ms_override: int | None = None,
) -> PeriodWindow:
    if not isinstance(request, PeriodRequest):
        request = PeriodRequest.from_mapping(request)
    request.validate()

    kind = str(request.period or "mtd").strip().lower()
    if kind not in _PERIOD_KINDS:
        raise ValueError("period must be one of: month, mtd, range, year, ytd")

    tz = ZoneInfo(REPORTING_TIMEZONE)
    effective_now_ms = int(now_ms if now_ms is not None else datetime.now(tz).timestamp() * 1000)
    now_local = datetime.fromtimestamp(effective_now_ms / 1000, tz=tz)
    today = now_local.date()

    if kind in {"mtd", "ytd"}:
        end_date = _parse_date(request.as_of_date, field_name="as_of_date") if request.as_of_date else today
        if end_date > today:
            raise ValueError("as_of_date cannot be in the future")
        start_date = date(end_date.year, end_date.month if kind == "mtd" else 1, 1)
    elif kind == "month":
        year, month = _parse_month(request.month)
        start_date = date(year, month, 1)
        current_month = date(today.year, today.month, 1)
        if start_date > current_month:
            raise ValueError("month cannot be in the future")
        natural_end_exclusive_date = _next_month_start(year, month)
        end_date = today if start_date == current_month else natural_end_exclusive_date - timedelta(days=1)
    elif kind == "year":
        year = _parse_year(request.year)
        if year > today.year:
            raise ValueError("year cannot be in the future")
        start_date = date(year, 1, 1)
        end_date = today if year == today.year else date(year, 12, 31)
    else:
        start_date = _parse_date(request.start_date, field_name="start_date")
        end_date = _parse_date(request.end_date, field_name="end_date")
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        if end_date > today:
            raise ValueError("end_date cannot be in the future")

    start_at_ms = _local_midnight_ms(start_date, tz)
    current_ending = end_date == today
    natural_end_exclusive_ms = (
        effective_now_ms + 1 if current_ending else _local_midnight_ms(end_date + timedelta(days=1), tz)
    )
    status = "partial_current" if current_ending else "complete_past"

    if cutoff_ms_override is not None:
        cutoff_ms = int(cutoff_ms_override)
        if cutoff_ms > effective_now_ms:
            raise ValueError("cutoff_ms_override cannot be in the future")
        if cutoff_ms < start_at_ms or cutoff_ms >= natural_end_exclusive_ms:
            raise ValueError("cutoff_ms_override must fall inside the normalized period")
        end_exclusive_ms = cutoff_ms + 1
        status = "partial_cutoff"
    else:
        end_exclusive_ms = natural_end_exclusive_ms

    if end_exclusive_ms <= start_at_ms:
        raise ValueError("normalized period must have positive duration")

    return PeriodWindow(
        kind=kind,
        reporting_timezone=REPORTING_TIMEZONE,
        requested_start_date=start_date.isoformat(),
        requested_end_date=end_date.isoformat(),
        effective_start_at_ms=start_at_ms,
        effective_end_exclusive_at_ms=end_exclusive_ms,
        valuation_open_at_ms=start_at_ms - 1,
        valuation_end_at_ms=end_exclusive_ms - 1,
        status=status,
    )


__all__ = [
    "PeriodRequest",
    "PeriodWindow",
    "REPORTING_TIMEZONE",
    "normalize_period",
]
